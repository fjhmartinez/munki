import os
import shutil
import subprocess

from urlparse import urlparse

from munkilib.munkirepo import Repo


# NetFS share mounting code borrowed and liberally adapted from Michael Lynn's
# work here: https://gist.github.com/pudquick/1362a8908be01e23041d
try:
    import objc
    from CoreFoundation import CFURLCreateWithString

    class Attrdict(dict):
        '''Custom dict class'''
        __getattr__ = dict.__getitem__
        __setattr__ = dict.__setitem__

    NetFS = Attrdict()
    # Can cheat and provide 'None' for the identifier, it'll just use
    # frameworkPath instead
    # scan_classes=False means only add the contents of this Framework
    NetFS_bundle = objc.initFrameworkWrapper(
        'NetFS', frameworkIdentifier=None,
        frameworkPath=objc.pathForFramework('NetFS.framework'),
        globals=NetFS, scan_classes=False)

    # https://developer.apple.com/library/mac/documentation/Cocoa/Conceptual/
    # ObjCRuntimeGuide/Articles/ocrtTypeEncodings.html
    # Fix NetFSMountURLSync signature
    del NetFS['NetFSMountURLSync']
    objc.loadBundleFunctions(
        NetFS_bundle, NetFS, [('NetFSMountURLSync', 'i@@@@@@o^@')])
    NETFSMOUNTURLSYNC_AVAILABLE = True
except (ImportError, KeyError):
    NETFSMOUNTURLSYNC_AVAILABLE = False


class ShareMountException(Exception):
    '''An exception raised if share mounting failed'''
    pass


class ShareAuthenticationNeededException(ShareMountException):
    '''An exception raised if authentication is needed'''
    pass


def mount_share(share_url):
    '''Mounts a share at /Volumes, returns the mount point or raises an error'''
    sh_url = CFURLCreateWithString(None, share_url, None)
    # Set UI to reduced interaction
    open_options = {NetFS.kNAUIOptionKey: NetFS.kNAUIOptionNoUI}
    # Allow mounting sub-directories of root shares
    mount_options = {NetFS.kNetFSAllowSubMountsKey: True}
    # Mount!
    result, mountpoints = NetFS.NetFSMountURLSync(
        sh_url, None, None, None, open_options, mount_options, None)
    # Check if it worked
    if result != 0:
        if result in (-6600, errno.EINVAL, errno.ENOTSUP, errno.EAUTH):
            # -6600 is kNetAuthErrorInternal in NetFS.h 10.9+
            # errno.EINVAL is returned if an afp share needs a login in some
            #               versions of OS X
            # errno.ENOTSUP is returned if an afp share needs a login in some
            #               versions of OS X
            # errno.EAUTH is returned if authentication fails (SMB for sure)
            raise ShareAuthenticationNeededException()
        raise ShareMountException('Error mounting url "%s": %s, error %s'
                                  % (share_url, os.strerror(result), result))
    # Return the mountpath
    return str(mountpoints[0])


def mount_share_with_credentials(share_url, username, password):
    '''Mounts a share at /Volumes, returns the mount point or raises an error
    Include username and password as parameters, not in the share_path URL'''
    sh_url = CFURLCreateWithString(None, share_url, None)
    # Set UI to reduced interaction
    open_options = {NetFS.kNAUIOptionKey: NetFS.kNAUIOptionNoUI}
    # Allow mounting sub-directories of root shares
    mount_options = {NetFS.kNetFSAllowSubMountsKey: True}
    # Mount!
    result, mountpoints = NetFS.NetFSMountURLSync(
        sh_url, None, username, password, open_options, mount_options, None)
    # Check if it worked
    if result != 0:
        raise ShareMountException('Error mounting url "%s": %s, error %s'
                                  % (share_url, os.strerror(result), result))
    # Return the mountpath
    return str(mountpoints[0])


def mount_share_url(share_url):
    '''Mount a share url under /Volumes, prompting for password if needed
    Raises ShareMountException if there's an error'''
    try:
        mountpoint = mount_share(share_url)
    except ShareAuthenticationNeededException:
        username = raw_input('Username: ')
        password = getpass.getpass()
        mountpoint = mount_share_with_credentials(share_url, username, password)
    return mountpoint


class NewFileRepo(Repo):
    '''Handles local filesystem repo and repos mounted via filesharing'''
    
    def __init__(self, baseurl):
        '''Constructor'''
        self.baseurl = baseurl
        url_parts = urlparse(baseurl)
        self.url_scheme = url_parts.scheme
        if self.url_scheme == 'file':
            self.root = url_parts.path
        else:
            self.root = os.path.join('/Volumes', url_parts.path)
        self.we_mounted_repo = False

    def connect(self):
        '''If self.root is present, return. Otherwise try to mount the share
        url.'''
        if not os.path.exists(self.root) and self.url_scheme != 'file':
            print 'Attempting to mount fileshare %s:' % self.baseurl
            if NETFSMOUNTURLSYNC_AVAILABLE:
                try:
                    self.root = mount_share_url(self.baseurl)
                except ShareMountException, err:
                    print sys.stderr, err
                    return 
                else:
                    self.we_mounted_repo = True
            else:
                os.mkdir(self.root)
                if self.baseurl.startswith('afp:'):
                    cmd = ['/sbin/mount_afp', '-i', self.baseurl, self.root]
                elif self.baseurl.startswith('smb:'):
                    cmd = ['/sbin/mount_smbfs', self.baseurl[4:], self.root]
                elif self.baseurl.startswith('nfs://'):
                    cmd = ['/sbin/mount_nfs', self.baseurl[6:], self.root]
                else:
                    print >> sys.stderr, 'Unsupported filesystem URL!'
                    return
                retcode = subprocess.call(cmd)
                if retcode:
                    os.rmdir(self.root)
                else:
                    self.we_mounted_repo = True
        # mount attempt complete; check again for existence of self.root
        if not os.path.exists(self.root):
            raise SomeSortOfError

    def itemlist(self, kind):
        '''Returns a list of identifiers for each item of kind.
        Kind might be 'catalogs', 'manifests', 'pkgsinfo', 'pkgs', or 'icons'.
        For a file-backed repo this would be a list of pathnames.'''
        search_dir = os.path.join(self.root, kind)
        file_list = []
        for (dirpath, dummy_dirnames, filenames) in os.walk(search_dir):
            for name in filenames:
                abs_path = os.path.join(dirpath, name)
                rel_path = abs_path[len(search_dir):].lstrip("/")
                file_list.append(rel_path)
        return file_list

    def get(self, resource_identifier):
        '''Returns the content of item with given resource_identifier.
        For a file-backed repo, a resource_identifier of
        'pkgsinfo/apps/Firefox-52.0.plist' would return the contents of
        <repo_root>/pkgsinfo/apps/Firefox-52.0.plist.
        Avoid using this method with the 'pkgs' kind as it might return a
        really large blob of data.'''
        repo_filepath = os.path.join(self.root, resource_identifier)
        try:
            fileref = open(repo_filepath)
            data = fileref.read()
            fileref.close()
            return data
        except OSError, err:
            raise

    def get_to_local_file(self, resource_identifier, local_file_path):
        '''Gets the contents of item with given resource_identifier and saves
        it to local_file_path.
        For a file-backed repo, a resource_identifier
        of 'pkgsinfo/apps/Firefox-52.0.plist' would copy the contents of
        <repo_root>/pkgsinfo/apps/Firefox-52.0.plist to a local file given by
        local_file_path.'''
        repo_filepath = os.path.join(self.root, resource_identifier)
        try:
            shutil.copyfile(repo_filepath, local_file_path)
        except (OSError, IOError), err:
            raise

    def put(self, resource_identifier, content):
        '''Stores content on the repo based on resource_identifier.
        For a file-backed repo, a resource_identifier of
        'pkgsinfo/apps/Firefox-52.0.plist' would result in the content being
        saved to <repo_root>/pkgsinfo/apps/Firefox-52.0.plist.'''
        repo_filepath = os.path.join(self.root, resource_identifier)
        dir_path = os.path.dirname(repo_filepath)
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, 0755)
        try:
            fileref = open(repo_filepath, 'w')
            fileref.write(content)
            fileref.close()
        except OSError, err:
            raise

    def put_from_local_file(self, resource_identifier, local_file_path):
        '''Copies the content of local_file_path to the repo based on
        resource_identifier. For a file-backed repo, a resource_identifier
        of 'pkgsinfo/apps/Firefox-52.0.plist' would result in the content
        being saved to <repo_root>/pkgsinfo/apps/Firefox-52.0.plist.'''
        repo_filepath = os.path.join(self.root, resource_identifier)
        dir_path = os.path.dirname(repo_filepath)
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, 0755)
        try:
            shutil.copyfile(local_file_path, repo_filepath)
        except (OSError, IOError), err:
            raise

    def delete(self, resource_identifier):
        '''Deletes a repo object located by resource_identifier.
        For a file-backed repo, a resource_identifier of
        'pkgsinfo/apps/Firefox-52.0.plist' would result in the deletion of
        <repo_root>/pkgsinfo/apps/Firefox-52.0.plist.'''
        repo_filepath = os.path.join(self.root, resource_identifier)
        try:
            os.remove(repo_filepath)
        except OSError, err:
            raise
        