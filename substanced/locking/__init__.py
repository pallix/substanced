""" Advisory exclusive DAV-style locks for content objects.  When a resource is
locked, it is presumed that its SDI UI will display a warning to users who do
not hold the lock.  The locking service can also be used by add-ons such as DAV
implementations.  """

import datetime
import uuid

from persistent import Persistent

import colander
from colander.iso8601 import UTC
import deform_bootstrap
import deform.widget

from pyramid.security import has_permission
from pyramid.traversal import (
    find_root,
    resource_path,
    )
from pyramid.threadlocal import get_current_registry

from substanced.content import (
    content,
    service
    )
from substanced.folder import (
    _AutoNamingFolder,
    Folder,
    )
from substanced.interfaces import ReferenceType
from substanced.objectmap import (
    reference_target_property,
    reference_targetid_property,
    )
from substanced.property import PropertySheet
from substanced.util import (
    find_objectmap,
    find_service,
    get_oid,
    )
from substanced.schema import Schema

class WriteLock(ReferenceType):
    """ Represents a DAV-style writelock.  It's a Substance D reference type
    from resource object to lock object"""

class UserToLock(ReferenceType):
    """ A reference type which represents the relationship from a user to
    his set of locks """
    
class LockingError(Exception):
    def __init__(self, lock):
        self.lock = lock

class LockError(LockingError):
    """ Raised when a lock attempt cannot be performed due to an existing
    conflicting lock.  Instances of this class have a ``lock`` attribute which
    is a :class:`substanced.locking.Lock` object, representing the conflicting
    lock. """

class UnlockError(LockingError):
    """ Raised when an unlock attempt cannot be performed due to the owner
    suplied in the unlock request not matching the owner of the lock.
    Instances of this class have a ``lock`` attribute which is a
    :class:`substanced.locking.Lock` object, representing the conflicting lock.
    """

def now():
    return datetime.datetime.utcnow().replace(tzinfo=UTC)

class LockOwnerSchema(colander.SchemaNode):
    title = 'Owner'
    schema_type = colander.Int

    @property
    def widget(self):
        context = self.bindings['context']
        principals = find_service(context, 'principals')
        if principals is None:
            values = [] # fbo dump/load
        else:
            values = [(get_oid(group), name) for name, group in 
                      principals['users'].items()]
        return deform_bootstrap.widget.ChosenSingleWidget(values=values)

    def validator(self, node, value):
        context = self.bindings['context']
        objectmap = find_objectmap(context)
        if not value in objectmap.objectid_to_path:
            raise colander.Invalid(node, 'Not a valid userid %r' % value)

class LockResourceSchema(colander.SchemaNode):
    title = 'Resource Path'
    schema_type = colander.String
    widget = deform.widget.TextInputWidget()
    missing = colander.null

    def preparer(self, value):
        context = self.bindings['context']
        request = self.bindings['request']
        objectmap = find_objectmap(context)
        if value is colander.null:
            return colander.null
        try:
            resource = objectmap.object_for(tuple(value.split('/')))
        except ValueError:
            return None
        if not has_permission('sdi.lock', resource, request):
            return False
        return resource

    def validator(self, node, value):
        if value is None:
            raise colander.Invalid(node, 'Resource not found')
        if value is False:
            raise colander.Invalid(
                node,
                'You do not have permission to lock this resource'
                )
        
class LockSchema(Schema):
    ownerid = LockOwnerSchema()
    timeout = colander.SchemaNode(
        colander.Int(),
        validator=colander.Range(0),
        default=3600,
        title='Timeout (secs)',
        )
    last_refresh = colander.SchemaNode(
        colander.DateTime(),
        title='Last Refresh',
        default=now(),
        )
    resource = LockResourceSchema()

class LockPropertySheet(PropertySheet):
    schema = LockSchema()

    def get(self):
        result = PropertySheet.get(self)
        resource = result.get('resource')
        if resource is None:
            resource = colander.null
        else:
            resource = resource_path(resource)
        result['resource'] = resource
        return result

    def set(self, appstruct):
        resource = appstruct.get('resource')
        if resource is colander.null:
            appstruct['resource'] = None
        return PropertySheet.set(self, appstruct)

@content(
    'Lock',
    icon='icon-lock',
    add_view='add_lock',
    propertysheets = (
        ('', LockPropertySheet),
        )
    )
class Lock(Persistent):
    """ A persistent object representing a lock. """
    owner = reference_target_property(UserToLock)
    resource = reference_target_property(WriteLock)
    ownerid = reference_targetid_property(UserToLock)
    resourceid = reference_targetid_property(WriteLock)

    def __init__(self, timeout=3600, last_refresh=None):
        self.timeout = timeout
        # last_refresh must be a datetime.datetime object with a UTC tzinfo,
        # if provided
        if last_refresh is None:
            last_refresh = now()
        self.last_refresh = last_refresh

    def refresh(self, timeout=None, when=None): # "when" is for testing
        """ Refresh the lock.  If the timeout is not None, set the timeout for
        this lock too. """
        if timeout is not None:
            self.timeout = timeout
        if when is None: # pragma: no cover
            when = now()
        self.last_refresh = when

    def expires(self):
        """ Return a datetime object representing the point in time at which
        this lock will expire or has expired. """
        if self.timeout is None:
            return None
        return self.last_refresh + datetime.timedelta(seconds=self.timeout)

    def is_valid(self, when=None):
        """ Return True if this lock has not yet expired and its resource still
        exists """
        objectmap = find_objectmap(self)
        if objectmap is not None:
            # might be None if we're not yet seated
            if self.resourceid is None:
                return False
        if when is None: # pragma: no cover
            when = now()
        expires = self.expires()
        if expires is None:
            return True
        return expires >= when

    def commit_suicide(self):
        del self.__parent__[self.__name__]
        
@service(
    'Lock Service',
    icon='icon-briefcase',
    service_name='locks',
    add_view='add_lock_service',
    )
class LockService(Folder, _AutoNamingFolder):
    __sdi_addable__ = ('Lock',)

    def next_name(self, subobject):
        lock_id = str(uuid.uuid4())
        return lock_id

    def _get_ownerid(self, owner_or_ownerid):
        ownerid = get_oid(owner_or_ownerid, None)
        if ownerid is None:
            ownerid = owner_or_ownerid
        if not isinstance(ownerid, int):
            raise ValueError(
                'Bad value for owner_or_ownerid %r' % owner_or_ownerid
                )
        return ownerid

    def lock(
        self,
        resource,
        owner_or_ownerid,
        timeout=None,
        locktype=WriteLock,
        ):
        # NB: callers should ensure that the user has 'sdi.lock' permission
        # on the resource before calling

        objectmap = find_objectmap(self)
        ownerid = self._get_ownerid(owner_or_ownerid)
        locks = objectmap.targets(resource, locktype)
        for lock in locks:
            if lock.is_valid():
                if lock.ownerid == ownerid:
                    when = now()
                    lock.refresh(timeout, when)
                    return lock
                else:
                    raise LockError(lock)
            else:
                lock.commit_suicide()
                break
        registry = get_current_registry()
        lock = registry.content.create('Lock', timeout=timeout)
        lock.ownerid = ownerid
        lock.resource = resource
        self.add_next(lock)
        return lock

    def unlock(
        self,
        resource,
        owner_or_ownerid,
        locktype=WriteLock,
        ):

        # NB: callers should ensure that the user has 'sdi.lock' permission
        # on the resource before calling
        objectmap = find_objectmap(self)
        ownerid = self._get_ownerid(owner_or_ownerid)
        locks = objectmap.targets(resource, locktype)
        lock = None
        for lock in locks:
            if (not lock.is_valid()) or (lock.ownerid == ownerid):
                lock.commit_suicide()
                break
        else: # nobreak
            raise UnlockError(lock)

def _get_lock_service(resource):
    lockservice = find_service(resource, 'locks')
    if lockservice is None:
        registry = get_current_registry()
        lockservice = registry.content.create('Lock Service')
        root = find_root(resource)
        root.add_service('Lock Service', lockservice)
    return lockservice
    
def lock_resource(
    resource,
    owner_or_ownerid,
    timeout=None,
    locktype=WriteLock,
    ):
    """ Lock a resource using the lock service.  If the resource is already
    locked by the owner supplied as owner_or_ownerid, calling this function
    will refresh the lock.  If the resource is not already locked by another
    user, calling this function will create a new lock.  If the resource is
    already locked by a different user, a :class:`substanced.locking.LockError`
    will be raised.  This function has the side effect of creating a Lock
    Service in the Substance D root if one does not already exist.

    .. warning::

       Callers should assert that the owner has the ``sdi.lock`` permission
       against the resource before calling this function to ensure that a user
       can't lock a resource he is not permitted to.

    """
    locks = _get_lock_service(resource)
    return locks.lock(
        resource,
        owner_or_ownerid,
        timeout=timeout,
        locktype=locktype,
        )

def unlock_resource(
    resource,
    owner_or_ownerid,
    locktype=WriteLock,
    ):
    """
    Unlock a resource using the lock service.  If the resource is already
    locked by a user other than the owner supplied as owner_or_ownerid or the
    resource isn't already locked with this lock type, calling this function
    will raise a :class:`substanced.locking.LockError` exception.  Otherwise
    the lock will be removed.  This function has the side effect of creating a
    Lock Service in the Substance D root if one does not already exist.

        .. warning::

           Callers should assert that the owner has the ``sdi.lock`` permission
           against the resource before calling this function to ensure that a
           user can't lock a resource he is not permitted to.
           
    """

    locks = _get_lock_service(resource)
    return locks.unlock(resource, owner_or_ownerid, locktype=locktype)

def includeme(config): # pragma: no cover
    config.add_permission('sdi.lock')
    config.include('.views')
    