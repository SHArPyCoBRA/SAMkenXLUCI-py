# Copyright 2018 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Define local cache policies."""

import contextlib
import errno
import io
import logging
import os
import random
import string
import sys
import time

from utils import file_path
from utils import fs
from utils import lru
from utils import threading_utils
from utils import tools


# The file size to be used when we don't know the correct file size,
# generally used for .isolated files.
UNKNOWN_FILE_SIZE = None


def file_write(path, content_generator):
  """Writes file content as generated by content_generator.

  Creates the intermediary directory as needed.

  Returns the number of bytes written.

  Meant to be mocked out in unit tests.
  """
  file_path.ensure_tree(os.path.dirname(path))
  total = 0
  with fs.open(path, 'wb') as f:
    for d in content_generator:
      total += len(d)
      f.write(d)
  return total


def is_valid_file(path, size):
  """Returns if the given files appears valid.

  Currently it just checks the file exists and its size matches the expectation.
  """
  if size == UNKNOWN_FILE_SIZE:
    return fs.isfile(path)
  try:
    actual_size = fs.stat(path).st_size
  except OSError as e:
    logging.warning(
        'Can\'t read item %s, assuming it\'s invalid: %s',
        os.path.basename(path), e)
    return False
  if size != actual_size:
    logging.warning(
        'Found invalid item %s; %d != %d',
        os.path.basename(path), actual_size, size)
    return False
  return True


def trim_caches(caches, path, min_free_space, max_age_secs):
  """Trims multiple caches.

  The goal here is to coherently trim all caches in a coherent LRU fashion,
  deleting older items independent of which container they belong to.

  Two policies are enforced first:
  - max_age_secs
  - min_free_space

  Once that's done, then we enforce each cache's own policies.

  Returns:
    Slice containing the size of all items evicted.
  """
  min_ts = time.time() - max_age_secs if max_age_secs else 0
  free_disk = file_path.get_free_space(path) if min_free_space else 0
  total = []
  if min_ts or free_disk:
    while True:
      oldest = [(c, c.get_oldest()) for c in caches if len(c) > 0]
      if not oldest:
        break
      oldest.sort(key=lambda (_, ts): ts)
      c, ts = oldest[0]
      if ts >= min_ts and free_disk >= min_free_space:
        break
      total.append(c.remove_oldest())
      if min_free_space:
        free_disk = file_path.get_free_space(path)
  # Evaluate each cache's own policies.
  for c in caches:
    total.extend(c.trim())
  return total


def _get_recursive_size(path):
  """Returns the total data size for the specified path.

  This function can be surprisingly slow on OSX, so its output should be cached.
  """
  try:
    total = 0
    for root, _, files in fs.walk(path):
      for f in files:
        total += fs.lstat(os.path.join(root, f)).st_size
    return total
  except (IOError, OSError, UnicodeEncodeError) as exc:
    logging.warning('Exception while getting the size of %s:\n%s', path, exc)
    return None


class NamedCacheError(Exception):
  """Named cache specific error."""


class NoMoreSpace(Exception):
  """Not enough space to map the whole directory."""
  pass


class CachePolicies(object):
  def __init__(self, max_cache_size, min_free_space, max_items, max_age_secs):
    """Common caching policies for the multiple caches (isolated, named, cipd).

    Arguments:
    - max_cache_size: Trim if the cache gets larger than this value. If 0, the
                      cache is effectively a leak.
    - min_free_space: Trim if disk free space becomes lower than this value. If
                      0, it will unconditionally fill the disk.
    - max_items: Maximum number of items to keep in the cache. If 0, do not
                 enforce a limit.
    - max_age_secs: Maximum age an item is kept in the cache until it is
                    automatically evicted. Having a lot of dead luggage slows
                    everything down.
    """
    self.max_cache_size = max_cache_size
    self.min_free_space = min_free_space
    self.max_items = max_items
    self.max_age_secs = max_age_secs

  def __str__(self):
    return (
        'CachePolicies(max_cache_size=%s; max_items=%s; min_free_space=%s; '
        'max_age_secs=%s)') % (
            self.max_cache_size, self.max_items, self.min_free_space,
            self.max_age_secs)


class CacheMiss(Exception):
  """Raised when an item is not in cache."""
  def __init__(self, digest):
    self.digest = digest
    super(CacheMiss, self).__init__(
        'Item with digest %r is not found in cache' % digest)


class Cache(object):
  def __init__(self, cache_dir):
    if cache_dir is not None:
      assert isinstance(cache_dir, unicode), cache_dir
      assert file_path.isabs(cache_dir), cache_dir
    self.cache_dir = cache_dir
    self._lock = threading_utils.LockWithAssert()
    # Profiling values.
    self._added = []
    self._used = []

  def __nonzero__(self):
    """A cache is always True.

    Otherwise it falls back to __len__, which is surprising.
    """
    return True

  def __len__(self):
    """Returns the number of entries in the cache."""
    raise NotImplementedError()

  def __iter__(self):
    """Iterates over all the entries names."""
    raise NotImplementedError()

  def __contains__(self, name):
    """Returns if an entry is in the cache."""
    raise NotImplementedError()

  @property
  def total_size(self):
    """Returns the total size of the cache in bytes."""
    raise NotImplementedError()

  @property
  def added(self):
    """Returns a list of the size for each entry added."""
    with self._lock:
      return self._added[:]

  @property
  def used(self):
    """Returns a list of the size for each entry used."""
    with self._lock:
      return self._used[:]

  def get_oldest(self):
    """Returns timestamp of oldest cache entry or None.

    Returns:
      Timestamp of the oldest item.

    Used for manual trimming.
    """
    raise NotImplementedError()

  def remove_oldest(self):
    """Removes the oldest item from the cache.

    Returns:
      Size of the oldest item.

    Used for manual trimming.
    """
    raise NotImplementedError()

  def save(self):
    """Saves the current cache to disk."""
    raise NotImplementedError()

  def trim(self):
    """Enforces cache policies, then calls save().

    Returns:
      Slice with the size of evicted items.
    """
    raise NotImplementedError()

  def cleanup(self):
    """Deletes any corrupted item from the cache, then calls trim(), then
    save().

    It is assumed to take significantly more time than trim().
    """
    raise NotImplementedError()


class ContentAddressedCache(Cache):
  """Content addressed cache that stores objects temporarily.

  It can be accessed concurrently from multiple threads, so it should protect
  its internal state with some lock.
  """

  def __enter__(self):
    """Context manager interface."""
    # TODO(maruel): Remove.
    return self

  def __exit__(self, _exc_type, _exec_value, _traceback):
    """Context manager interface."""
    # TODO(maruel): Remove.
    return False

  def touch(self, digest, size):
    """Ensures item is not corrupted and updates its LRU position.

    Arguments:
      digest: hash digest of item to check.
      size: expected size of this item.

    Returns:
      True if item is in cache and not corrupted.
    """
    raise NotImplementedError()

  def getfileobj(self, digest):
    """Returns a readable file like object.

    If file exists on the file system it will have a .name attribute with an
    absolute path to the file.
    """
    raise NotImplementedError()

  def write(self, digest, content):
    """Reads data from |content| generator and stores it in cache.

    It is possible to write to an object that already exists. It may be
    ignored (sent to /dev/null) but the timestamp is still updated.

    Returns digest to simplify chaining.
    """
    raise NotImplementedError()


class MemoryContentAddressedCache(ContentAddressedCache):
  """ContentAddressedCache implementation that stores everything in memory."""

  def __init__(self, file_mode_mask=0500):
    """Args:
      file_mode_mask: bit mask to AND file mode with. Default value will make
          all mapped files to be read only.
    """
    super(MemoryContentAddressedCache, self).__init__(None)
    self._file_mode_mask = file_mode_mask
    # Items in a LRU lookup dict(digest: size).
    self._lru = lru.LRUDict()

  # Cache interface implementation.

  def __len__(self):
    with self._lock:
      return len(self._lru)

  def __iter__(self):
    # This is not thread-safe.
    return self._lru.__iter__()

  def __contains__(self, digest):
    with self._lock:
      return digest in self._lru

  @property
  def total_size(self):
    with self._lock:
      return sum(len(i) for i in self._lru.itervalues())

  def get_oldest(self):
    with self._lock:
      try:
        # (key, (value, ts))
        return self._lru.get_oldest()[1][1]
      except KeyError:
        return None

  def remove_oldest(self):
    with self._lock:
      # TODO(maruel): Update self._added.
      # (key, (value, ts))
      return len(self._lru.pop_oldest()[1][0])

  def save(self):
    pass

  def trim(self):
    """Trimming is not implemented for MemoryContentAddressedCache."""
    return []

  def cleanup(self):
    """Cleaning is irrelevant, as there's no stateful serialization."""
    pass

  # ContentAddressedCache interface implementation.

  def __contains__(self, digest):
    with self._lock:
      return digest in self._lru

  def touch(self, digest, size):
    with self._lock:
      try:
        self._lru.touch(digest)
      except KeyError:
        return False
      return True

  def getfileobj(self, digest):
    with self._lock:
      try:
        d = self._lru[digest]
      except KeyError:
        raise CacheMiss(digest)
      self._used.append(len(d))
      self._lru.touch(digest)
    return io.BytesIO(d)

  def write(self, digest, content):
    # Assemble whole stream before taking the lock.
    data = ''.join(content)
    with self._lock:
      self._lru.add(digest, data)
      self._added.append(len(data))
    return digest


class DiskContentAddressedCache(ContentAddressedCache):
  """Stateful LRU cache in a flat hash table in a directory.

  Saves its state as json file.
  """
  STATE_FILE = u'state.json'

  def __init__(self, cache_dir, policies, hash_algo, trim, time_fn=None):
    """
    Arguments:
      cache_dir: directory where to place the cache.
      policies: CachePolicies instance, cache retention policies.
      algo: hashing algorithm used.
      trim: if True to enforce |policies| right away.
        It can be done later by calling trim() explicitly.
    """
    # All protected methods (starting with '_') except _path should be called
    # with self._lock held.
    super(DiskContentAddressedCache, self).__init__(cache_dir)
    self.policies = policies
    self.hash_algo = hash_algo
    self.state_file = os.path.join(cache_dir, self.STATE_FILE)
    # Items in a LRU lookup dict(digest: size).
    self._lru = lru.LRUDict()
    # Current cached free disk space. It is updated by self._trim().
    file_path.ensure_tree(self.cache_dir)
    self._free_disk = file_path.get_free_space(self.cache_dir)
    # The first item in the LRU cache that must not be evicted during this run
    # since it was referenced. All items more recent that _protected in the LRU
    # cache are also inherently protected. It could be a set() of all items
    # referenced but this increases memory usage without a use case.
    self._protected = None
    # Cleanup operations done by self._load(), if any.
    self._operations = []
    with tools.Profiler('Setup'):
      with self._lock:
        self._load(trim, time_fn)

  # Cache interface implementation.

  def __len__(self):
    with self._lock:
      return len(self._lru)

  def __iter__(self):
    # This is not thread-safe.
    return self._lru.__iter__()

  def __contains__(self, digest):
    with self._lock:
      return digest in self._lru

  @property
  def total_size(self):
    with self._lock:
      return sum(self._lru.itervalues())

  def get_oldest(self):
    with self._lock:
      try:
        # (key, (value, ts))
        return self._lru.get_oldest()[1][1]
      except KeyError:
        return None

  def remove_oldest(self):
    with self._lock:
      # TODO(maruel): Update self._added.
      return self._remove_lru_file(True)

  def save(self):
    with self._lock:
      return self._save()

  def trim(self):
    """Forces retention policies."""
    with self._lock:
      return self._trim()

  def cleanup(self):
    """Cleans up the cache directory.

    Ensures there is no unknown files in cache_dir.
    Ensures the read-only bits are set correctly.

    At that point, the cache was already loaded, trimmed to respect cache
    policies.
    """
    with self._lock:
      fs.chmod(self.cache_dir, 0700)
      # Ensure that all files listed in the state still exist and add new ones.
      previous = set(self._lru)
      # It'd be faster if there were a readdir() function.
      for filename in fs.listdir(self.cache_dir):
        if filename == self.STATE_FILE:
          fs.chmod(os.path.join(self.cache_dir, filename), 0600)
          continue
        if filename in previous:
          fs.chmod(os.path.join(self.cache_dir, filename), 0400)
          previous.remove(filename)
          continue

        # An untracked file. Delete it.
        logging.warning('Removing unknown file %s from cache', filename)
        p = self._path(filename)
        if fs.isdir(p):
          try:
            file_path.rmtree(p)
          except OSError:
            pass
        else:
          file_path.try_remove(p)
        continue

      if previous:
        # Filter out entries that were not found.
        logging.warning('Removed %d lost files', len(previous))
        for filename in previous:
          self._lru.pop(filename)
        self._save()

    # What remains to be done is to hash every single item to
    # detect corruption, then save to ensure state.json is up to date.
    # Sadly, on a 50GiB cache with 100MiB/s I/O, this is still over 8 minutes.
    # TODO(maruel): Let's revisit once directory metadata is stored in
    # state.json so only the files that had been mapped since the last cleanup()
    # call are manually verified.
    #
    #with self._lock:
    #  for digest in self._lru:
    #    if not isolated_format.is_valid_hash(
    #        self._path(digest), self.hash_algo):
    #      self.evict(digest)
    #      logging.info('Deleted corrupted item: %s', digest)

  # ContentAddressedCache interface implementation.

  def __contains__(self, digest):
    with self._lock:
      return digest in self._lru

  def touch(self, digest, size):
    """Verifies an actual file is valid and bumps its LRU position.

    Returns False if the file is missing or invalid.

    Note that is doesn't compute the hash so it could still be corrupted if the
    file size didn't change.
    """
    # Do the check outside the lock.
    looks_valid = is_valid_file(self._path(digest), size)

    # Update its LRU position.
    with self._lock:
      if digest not in self._lru:
        if looks_valid:
          # Exists but not in the LRU anymore.
          self._delete_file(digest, size)
        return False
      if not looks_valid:
        self._lru.pop(digest)
        # Exists but not in the LRU anymore.
        self._delete_file(digest, size)
        return False
      self._lru.touch(digest)
      self._protected = self._protected or digest
    return True

  def getfileobj(self, digest):
    try:
      f = fs.open(self._path(digest), 'rb')
    except IOError:
      raise CacheMiss(digest)
    with self._lock:
      try:
        self._used.append(self._lru[digest])
      except KeyError:
        # If the digest is not actually in _lru, assume it is a cache miss.
        # Existing file will be overwritten by whoever uses the cache and added
        # to _lru.
        f.close()
        raise CacheMiss(digest)
    return f

  def write(self, digest, content):
    assert content is not None
    with self._lock:
      self._protected = self._protected or digest
    path = self._path(digest)
    # A stale broken file may remain. It is possible for the file to have write
    # access bit removed which would cause the file_write() call to fail to open
    # in write mode. Take no chance here.
    file_path.try_remove(path)
    try:
      size = file_write(path, content)
    except:
      # There are two possible places were an exception can occur:
      #   1) Inside |content| generator in case of network or unzipping errors.
      #   2) Inside file_write itself in case of disk IO errors.
      # In any case delete an incomplete file and propagate the exception to
      # caller, it will be logged there.
      file_path.try_remove(path)
      raise
    # Make the file read-only in the cache.  This has a few side-effects since
    # the file node is modified, so every directory entries to this file becomes
    # read-only. It's fine here because it is a new file.
    file_path.set_read_only(path, True)
    with self._lock:
      self._add(digest, size)
    return digest

  # Internal functions.

  def _load(self, trim, time_fn):
    """Loads state of the cache from json file.

    If cache_dir does not exist on disk, it is created.
    """
    self._lock.assert_locked()

    if not fs.isfile(self.state_file):
      if not fs.isdir(self.cache_dir):
        fs.makedirs(self.cache_dir)
    else:
      # Load state of the cache.
      try:
        self._lru = lru.LRUDict.load(self.state_file)
      except ValueError as err:
        logging.error('Failed to load cache state: %s' % (err,))
        # Don't want to keep broken state file.
        file_path.try_remove(self.state_file)
    if time_fn:
      self._lru.time_fn = time_fn
    if trim:
      self._trim()

  def _save(self):
    """Saves the LRU ordering."""
    self._lock.assert_locked()
    if sys.platform != 'win32':
      d = os.path.dirname(self.state_file)
      if fs.isdir(d):
        # Necessary otherwise the file can't be created.
        file_path.set_read_only(d, False)
    if fs.isfile(self.state_file):
      file_path.set_read_only(self.state_file, False)
    self._lru.save(self.state_file)

  def _trim(self):
    """Trims anything we don't know, make sure enough free space exists."""
    self._lock.assert_locked()
    evicted = []

    # Trim old items.
    if self.policies.max_age_secs:
      cutoff = self._lru.time_fn() - self.policies.max_age_secs
      while self._lru:
        oldest = self._lru.get_oldest()
        # (key, (data, ts)
        if oldest[1][1] >= cutoff:
          break
        evicted.append(self._remove_lru_file(True))

    # Ensure maximum cache size.
    if self.policies.max_cache_size:
      total_size = sum(self._lru.itervalues())
      while total_size > self.policies.max_cache_size:
        e = self._remove_lru_file(True)
        evicted.append(e)
        total_size -= e

    # Ensure maximum number of items in the cache.
    if self.policies.max_items and len(self._lru) > self.policies.max_items:
      for _ in xrange(len(self._lru) - self.policies.max_items):
        evicted.append(self._remove_lru_file(True))

    # Ensure enough free space.
    self._free_disk = file_path.get_free_space(self.cache_dir)
    while (
        self.policies.min_free_space and
        self._lru and
        self._free_disk < self.policies.min_free_space):
      # self._free_disk is updated by this call.
      evicted.append(self._remove_lru_file(True))

    if evicted:
      total_usage = sum(self._lru.itervalues())
      usage_percent = 0.
      if total_usage:
        usage_percent = 100. * float(total_usage) / self.policies.max_cache_size

      logging.warning(
          'Trimmed %d file(s) (%.1fkb) due to not enough free disk space:'
          ' %.1fkb free, %.1fkb cache (%.1f%% of its maximum capacity of '
          '%.1fkb)',
          len(evicted), sum(evicted) / 1024.,
          self._free_disk / 1024.,
          total_usage / 1024.,
          usage_percent,
          self.policies.max_cache_size / 1024.)
    self._save()
    return evicted

  def _path(self, digest):
    """Returns the path to one item."""
    return os.path.join(self.cache_dir, digest)

  def _remove_lru_file(self, allow_protected):
    """Removes the lastest recently used file and returns its size.

    Updates self._free_disk.
    """
    self._lock.assert_locked()
    try:
      digest, (size, _ts) = self._lru.get_oldest()
      if not allow_protected and digest == self._protected:
        total_size = sum(self._lru.itervalues())+size
        msg = (
            'Not enough space to fetch the whole isolated tree.\n'
            '  %s\n  cache=%dbytes, %d items; %sb free_space') % (
              self.policies, total_size, len(self._lru)+1, self._free_disk)
        raise NoMoreSpace(msg)
    except KeyError:
      # That means an internal error.
      raise NoMoreSpace('Nothing to remove, can\'t happend')
    digest, (size, _) = self._lru.pop_oldest()
    logging.debug('Removing LRU file %s', digest)
    self._delete_file(digest, size)
    return size

  def _add(self, digest, size=UNKNOWN_FILE_SIZE):
    """Adds an item into LRU cache marking it as a newest one."""
    self._lock.assert_locked()
    if size == UNKNOWN_FILE_SIZE:
      size = fs.stat(self._path(digest)).st_size
    self._added.append(size)
    self._lru.add(digest, size)
    self._free_disk -= size
    # Do a quicker version of self._trim(). It only enforces free disk space,
    # not cache size limits. It doesn't actually look at real free disk space,
    # only uses its cache values. self._trim() will be called later to enforce
    # real trimming but doing this quick version here makes it possible to map
    # an isolated that is larger than the current amount of free disk space when
    # the cache size is already large.
    while (
        self.policies.min_free_space and
        self._lru and
        self._free_disk < self.policies.min_free_space):
      # self._free_disk is updated by this call.
      if self._remove_lru_file(False) == -1:
        break

  def _delete_file(self, digest, size=UNKNOWN_FILE_SIZE):
    """Deletes cache file from the file system.

    Updates self._free_disk.
    """
    self._lock.assert_locked()
    try:
      if size == UNKNOWN_FILE_SIZE:
        try:
          size = fs.stat(self._path(digest)).st_size
        except OSError:
          size = 0
      if file_path.try_remove(self._path(digest)):
        self._free_disk += size
    except OSError as e:
      if e.errno != errno.ENOENT:
        logging.error('Error attempting to delete a file %s:\n%s' % (digest, e))


class NamedCache(Cache):
  """Manages cache directories.

  A cache entry is a tuple (name, path), where
    name is a short identifier that describes the contents of the cache, e.g.
      "git_v8" could be all git repositories required by v8 builds, or
      "build_chromium" could be build artefacts of the Chromium.
    path is a directory path relative to the task run dir. Cache installation
      puts the requested cache directory at the path.
  """
  _DIR_ALPHABET = string.ascii_letters + string.digits
  STATE_FILE = u'state.json'
  NAMED_DIR = u'named'

  def __init__(self, cache_dir, policies, time_fn=None):
    """Initializes NamedCaches.

    Arguments:
    - cache_dir is a directory for persistent cache storage.
    - policies is a CachePolicies instance.
    - time_fn is a function that returns timestamp (float) and used to take
      timestamps when new caches are requested. Used in unit tests.
    """
    super(NamedCache, self).__init__(cache_dir)
    self._policies = policies
    # LRU {cache_name -> tuple(cache_location, size)}
    self.state_file = os.path.join(cache_dir, self.STATE_FILE)
    self._lru = lru.LRUDict()
    if not fs.isdir(self.cache_dir):
      fs.makedirs(self.cache_dir)
    elif os.path.isfile(self.state_file):
      try:
        self._lru = lru.LRUDict.load(self.state_file)
      except ValueError:
        logging.exception(
            'NamedCache: failed to load named cache state file; obliterating')
        file_path.rmtree(self.cache_dir)
      with self._lock:
        self._try_upgrade()
    if time_fn:
      self._lru.time_fn = time_fn

  @property
  def available(self):
    """Returns a set of names of available caches."""
    with self._lock:
      return set(self._lru)

  def install(self, path, name):
    """Moves the directory for the specified named cache to |path|.

    path must be absolute, unicode and must not exist.

    Raises NamedCacheError if cannot install the cache.
    """
    logging.info('NamedCache.install(%r, %r)', path, name)
    with self._lock:
      try:
        if os.path.isdir(path):
          raise NamedCacheError(
              'installation directory %r already exists' % path)

        link_name = os.path.join(self.cache_dir, name)
        if fs.exists(link_name):
          fs.rmtree(link_name)

        if name in self._lru:
          rel_cache, size = self._lru.get(name)
          abs_cache = os.path.join(self.cache_dir, rel_cache)
          if os.path.isdir(abs_cache):
            logging.info('- reusing %r; size was %d', rel_cache, size)
            file_path.ensure_tree(os.path.dirname(path))
            fs.rename(abs_cache, path)
            self._remove(name)
            return

          logging.warning('- expected directory %r, does not exist', rel_cache)
          self._remove(name)

        # The named cache does not exist, create an empty directory. When
        # uninstalling, we will move it back to the cache and create an an
        # entry.
        logging.info('- creating new directory')
        file_path.ensure_tree(path)
      except (IOError, OSError) as ex:
        raise NamedCacheError(
            'cannot install cache named %r at %r: %s' % (
              name, path, ex))
      finally:
        self._save()

  def uninstall(self, path, name):
    """Moves the cache directory back. Opposite to install().

    path must be absolute and unicode.

    Raises NamedCacheError if cannot uninstall the cache.
    """
    logging.info('NamedCache.uninstall(%r, %r)', path, name)
    with self._lock:
      try:
        if not os.path.isdir(path):
          logging.warning(
              'NamedCache: Directory %r does not exist anymore. Cache lost.',
              path)
          return

        if name in self._lru:
          # This shouldn't happen but just remove the preexisting one and move
          # on.
          logging.error('- overwriting existing cache!')
          self._remove(name)
        rel_cache = self._allocate_dir()

        # Move the dir and create an entry for the named cache.
        abs_cache = os.path.join(self.cache_dir, rel_cache)
        logging.info('- Moving to %r', rel_cache)
        file_path.ensure_tree(os.path.dirname(abs_cache))
        fs.rename(path, abs_cache)

        # That succeeded, calculate its new size.
        size = _get_recursive_size(abs_cache)
        if not size:
          # Do not save empty named cache.
          return
        logging.info('- Size is %d', size)
        self._lru.add(name, (rel_cache, size))
        self._added.append(size)

        # Create symlink <cache_dir>/<named>/<name> -> <cache_dir>/<short name>
        # for user convenience.
        named_path = self._get_named_path(name)
        if os.path.exists(named_path):
          file_path.remove(named_path)
        else:
          file_path.ensure_tree(os.path.dirname(named_path))

        try:
          fs.symlink(abs_cache, named_path)
          logging.info(
              'NamedCache: Created symlink %r to %r', named_path, abs_cache)
        except OSError:
          # Ignore on Windows. It happens when running as a normal user or when
          # UAC is enabled and the user is a filtered administrator account.
          if sys.platform != 'win32':
            raise
      except (IOError, OSError) as ex:
        raise NamedCacheError(
            'cannot uninstall cache named %r at %r: %s' % (
              name, path, ex))
      finally:
        # Call save() at every uninstall. The assumptions are:
        # - The total the number of named caches is low, so the state.json file
        #   is small, so the time it takes to write it to disk is short.
        # - The number of mapped named caches per task is low, so the number of
        #   times save() is called on tear-down isn't high enough to be
        #   significant.
        # - uninstall() sometimes throws due to file locking on Windows or
        #   access rights on Linux. We want to keep as many as possible.
        self._save()

  # Cache interface implementation.

  def __len__(self):
    with self._lock:
      return len(self._lru)

  def __iter__(self):
    # This is not thread-safe.
    return self._lru.__iter__()

  def __contains__(self, digest):
    with self._lock:
      return digest in self._lru

  @property
  def total_size(self):
    with self._lock:
      return sum(size for _rel_path, size in self._lru.itervalues())

  def get_oldest(self):
    with self._lock:
      try:
        # (key, (value, ts))
        return self._lru.get_oldest()[1][1]
      except KeyError:
        return None

  def remove_oldest(self):
    with self._lock:
      # TODO(maruel): Update self._added.
      _name, size = self._remove_lru_item()
      return size

  def save(self):
    with self._lock:
      return self._save()

  def trim(self):
    evicted = []
    with self._lock:
      if not os.path.isdir(self.cache_dir):
        return evicted

      # Trim according to maximum number of items.
      if self._policies.max_items:
        while len(self._lru) > self._policies.max_items:
          name, size = self._remove_lru_item()
          evicted.append(size)
          logging.info(
              'NamedCache.trim(): Removed %r(%d) due to max_items(%d)',
              name, size, self._policies.max_items)

      # Trim according to maximum age.
      if self._policies.max_age_secs:
        cutoff = self._lru.time_fn() - self._policies.max_age_secs
        while self._lru:
          _name, (_data, ts) = self._lru.get_oldest()
          if ts >= cutoff:
            break
          name, size = self._remove_lru_item()
          evicted.append(size)
          logging.info(
              'NamedCache.trim(): Removed %r(%d) due to max_age_secs(%d)',
              name, size, self._policies.max_age_secs)

      # Trim according to minimum free space.
      if self._policies.min_free_space:
        while self._lru:
          free_space = file_path.get_free_space(self.cache_dir)
          if free_space >= self._policies.min_free_space:
            break
          name, size = self._remove_lru_item()
          evicted.append(size)
          logging.info(
              'NamedCache.trim(): Removed %r(%d) due to min_free_space(%d)',
              name, size, self._policies.min_free_space)

      # Trim according to maximum total size.
      if self._policies.max_cache_size:
        while self._lru:
          total = sum(size for _rel_cache, size in self._lru.itervalues())
          if total <= self._policies.max_cache_size:
            break
          name, size = self._remove_lru_item()
          evicted.append(size)
          logging.info(
              'NamedCache.trim(): Removed %r(%d) due to max_cache_size(%d)',
              name, size, self._policies.max_cache_size)

      self._save()
    return evicted

  def cleanup(self):
    """Removes unknown directories.

    Does not recalculate the cache size since it's surprisingly slow on some
    OSes.
    """
    success = True
    with self._lock:
      try:
        actual = set(fs.listdir(self.cache_dir))
        actual.discard(self.NAMED_DIR)
        actual.discard(self.STATE_FILE)
        expected = {v[0]: k for k, v in self._lru.iteritems()}
        # First, handle the actual cache content.
        # Remove missing entries.
        for missing in (set(expected) - actual):
          name, size = self._lru.pop(expected[missing])
          logging.warning(
              'NamedCache.cleanup(): Missing on disk %r(%d)', name, size)
        # Remove unexpected items.
        for unexpected in (actual - set(expected)):
          try:
            p = os.path.join(self.cache_dir, unexpected)
            logging.warning(
                'NamedCache.cleanup(): Unexpected %r', unexpected)
            if fs.isdir(p) and not fs.islink(p):
              file_path.rmtree(p)
            else:
              fs.remove(p)
          except (IOError, OSError) as e:
            logging.error('Failed to remove %s: %s', unexpected, e)
            success = False

        # Second, fix named cache links.
        named = os.path.join(self.cache_dir, self.NAMED_DIR)
        if os.path.isdir(named):
          actual = set(fs.listdir(named))
          expected = set(self._lru)
          # Confirm entries. Do not add missing ones for now.
          for name in expected.intersection(actual):
            p = os.path.join(self.cache_dir, self.NAMED_DIR, name)
            expected_link = os.path.join(self.cache_dir, self._lru[name][0])
            if fs.islink(p):
              if sys.platform == 'win32':
                # TODO(maruel): Implement readlink() on Windows in fs.py, then
                # remove this condition.
                # https://crbug.com/853721
                continue
              link = fs.readlink(p)
              if expected_link == link:
                continue
              logging.warning(
                  'Unexpected symlink for cache %s: %s, expected %s',
                  name, link, expected_link)
            else:
              logging.warning('Unexpected non symlink for cache %s', name)
            if fs.isdir(p) and not fs.islink(p):
              file_path.rmtree(p)
            else:
              fs.remove(p)
          # Remove unexpected items.
          for unexpected in (actual - expected):
            try:
              p = os.path.join(self.cache_dir, self.NAMED_DIR, unexpected)
              if fs.isdir(p):
                file_path.rmtree(p)
              else:
                fs.remove(p)
            except (IOError, OSError) as e:
              logging.error('Failed to remove %s: %s', unexpected, e)
              success = False
      finally:
        self._save()
    return success

  # Internal functions.

  def _try_upgrade(self):
    """Upgrades from the old format to the new one if necessary.

    This code can be removed so all bots are known to have the right new format.
    """
    if not self._lru:
      return
    _name, (data, _ts) = self._lru.get_oldest()
    if isinstance(data, (list, tuple)):
      return
    # Update to v2.
    def upgrade(_name, rel_cache):
      abs_cache = os.path.join(self.cache_dir, rel_cache)
      return rel_cache, _get_recursive_size(abs_cache)
    self._lru.transform(upgrade)
    self._save()

  def _remove_lru_item(self):
    """Removes the oldest LRU entry. LRU must not be empty."""
    name, ((_rel_path, size), _ts) = self._lru.get_oldest()
    logging.info('Removing named cache %r', name)
    self._remove(name)
    return name, size

  def _allocate_dir(self):
    """Creates and returns relative path of a new cache directory."""
    # We randomly generate directory names that have two lower/upper case
    # letters or digits. Total number of possibilities is (26*2 + 10)^2 = 3844.
    abc_len = len(self._DIR_ALPHABET)
    tried = set()
    while len(tried) < 1000:
      i = random.randint(0, abc_len * abc_len - 1)
      rel_path = (
        self._DIR_ALPHABET[i / abc_len] +
        self._DIR_ALPHABET[i % abc_len])
      if rel_path in tried:
        continue
      abs_path = os.path.join(self.cache_dir, rel_path)
      if not fs.exists(abs_path):
        return rel_path
      tried.add(rel_path)
    raise NamedCacheError(
        'could not allocate a new cache dir, too many cache dirs')

  def _remove(self, name):
    """Removes a cache directory and entry.

    Returns:
      Number of caches deleted.
    """
    self._lock.assert_locked()
    # First try to remove the alias if it exists.
    named_dir = self._get_named_path(name)
    if fs.islink(named_dir):
      fs.unlink(named_dir)

    # Then remove the actual data.
    if name not in self._lru:
      return
    rel_path, _size = self._lru.get(name)
    abs_path = os.path.join(self.cache_dir, rel_path)
    if os.path.isdir(abs_path):
      file_path.rmtree(abs_path)
    self._lru.pop(name)

  def _save(self):
    self._lock.assert_locked()
    file_path.ensure_tree(self.cache_dir)
    self._lru.save(self.state_file)

  def _get_named_path(self, name):
    return os.path.join(self.cache_dir, self.NAMED_DIR, name)
