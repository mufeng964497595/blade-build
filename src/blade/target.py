# Copyright (c) 2011 Tencent Inc.
# All rights reserved.
#
# Author: Michaelpeng <michaelpeng@tencent.com>
# Date:   October 20, 2011


"""
This is the target module which is the super class of all of the targets.
"""

from __future__ import absolute_import
from __future__ import print_function

import os
import re

from blade import config
from blade import console
from blade import target_pattern
from blade.util import var_to_list, iteritems, source_location, md5sum


def _is_likely_concatenated_filenames(string, exts):
    """Check whether a string is likely a concatenated filenames.
    This situation is usually caused by missing a comma between file names.
    For example, if the user writes:
        ```
        [
            'first.h',
            'second.h'  # NOTE: Missing the ending ","
            'third.h',
        ]
        ```
        'second.h' will be concatenated with 'third.h' to be 'first.hsecond.h'
    """
    # Convert exts to regex, e.g., ['h', 'hpp'] to "(h|hpp)"
    ext_pattern = '(%s)' % '|'.join(e.replace('+', r'\+') for e in exts)
    return re.search(r'\w+\.{ext}.+\.{ext}$'.format(ext=ext_pattern), string)


# Target regex
_TARGET_RE = re.compile(r'(?P<path>((//)?[\w./+-]+)?:|#)(?P<name>[\w.+-]*)$')

# Location reference macro regex
LOCATION_RE = re.compile(r'\$\(location\s+(\S*:\S+)(\s+\w*)?\)')


def _check_path(path):
    msg = []
    if path.startswith('//'):
        path = path[2:]
    if path.startswith('/'):
        msg.append('absolute path is not allowed')
        result = False
    if '..' in path:
        msg.append('parent path ".." is not allowed')
    return msg


def _parse_target(dep):
    """Parse a dep target into (path, name).

    Returns:
        Return according result for different form of dep:
        - ('', '', error_messages) for invalid,
        - (//path, name, '') for '//path:name',
        - (path, name, '') for 'path:name,',
        - ('', name, '') for ':name',
        - ('#', name, '') for '#name'.

    For the sake of performance, there is a cache.
    """
    if dep in _parse_target.cache:
        return _parse_target.cache[dep]
    match = _TARGET_RE.match(dep)
    if not match:
        msg = 'format error'
        if dep.count(':') > 1:
            msg += ', missing "," between targets?'
        msgs = [msg]
    else:
        path = match.group('path').rstrip(':')
        name = match.group('name')
        msgs = _check_path(path)
        if not name:
            msgs.append('empty name')
    if msgs:
        result = ('', '', msgs)
    else:
        if path:
            path = os.path.normpath(path)
        result = (path, name, None)
    _parse_target.cache[dep] = result
    return result


_parse_target.cache = {}


class Target(object):
    """Abstract target class.

    This class should be derived by subclass like CcLibrary CcBinary
    targets, etc.

    """

    def __init__(self,
                 name,
                 type,
                 srcs,
                 src_exts,
                 deps,
                 visibility,
                 kwargs):
        """Init method.

        Init the target.

        """
        from blade import build_manager  # pylint: disable=import-outside-toplevel
        self.blade = build_manager.instance
        self.target_database = self.blade.get_target_database()

        self.type = type
        self.name = name

        current_source_path = self.blade.get_current_source_path()
        self.path = current_source_path
        self.build_dir = self.blade.get_build_dir()
        self.target_dir = os.path.normpath(os.path.join(self.build_dir, current_source_path))

        # The unique key of this target, for internal use mainly.
        self.key = '%s:%s' % (current_source_path, name)
        # The full qualified target id, to be displayed in diagnostic message
        self.fullname = '//' + self.key
        self.source_location = source_location(os.path.join(current_source_path, 'BUILD'))
        self.srcs = srcs
        self.deps = []

        # Expanded dependencies, includes direct and indirect dependies.
        self.expanded_deps = []    # Provide type info then make lints happy(not-an-iterable).
        self.expanded_deps = None  # Set to None to indicate not constructed.

        self.dependents = set()  # Target keys which depends on this
        self.expanded_dependents = set()  # Expanded target keys which depends on this
        self._implicit_deps = set()
        self._visibility = set()
        self._visibility_is_default = True

        if not name:
            self.fatal('Missing "name"')

        # Keep track of target filess generated by this target. Note that one target rule
        # may correspond to several target files, such as:
        # proto_library: static lib/shared lib/jar variables
        self.__targets = {}
        self.__default_target = ''
        self.__clean_list = []  # Paths to be cleaned

        # Target releated attributes, they should be set only before generating build rules.
        self.attr = {}

        # For temporary, mutable fields only, their values should not relate to fingerprint
        self.data = {}

        # TODO: Remove it, make a `TestTargetMixin`
        self.attr['test_timeout'] = config.get_item('global_config', 'test_timeout')

        self._check_name()
        self._check_kwargs(kwargs)
        self._check_srcs(src_exts)
        self._init_target_deps(deps)
        self._init_visibility(visibility)
        self.__build_code = None
        self.__fingerprint = None  # Cached fingerprint

    def dump(self):
        """Dump to a dict"""
        target = {
            'type': self.type,
            'path': self.path,
            'name': self.name,
            'srcs': self.srcs,
            'deps': self.deps,
            'visibility': list(self._visibility),
        }
        target.update(self.attr)
        return target

    def _fingerprint_entropy(self):
        """
        Add more entropy to fingerprint.

        Can be override in sub classes, must return a dict{string:value}.

        The default implementation is return the `attr` member, but you can return lesser or more
        elements to custom the final result.
        For example, you can remove unrelated members in `attr` which doesn't affect build and must
        add extra elements which may affect build.
        """
        return self.attr

    def fingerprint(self):
        """Calculate a hash string to be used to judge whether regenerate per-target ninja file"""
        if self.__fingerprint is None:
            # All build related factors should be added to avoid outdated ninja file beeing used.
            entropy = {
                'blade_revision': self.blade.revision(),
                'config': config.digest(),
                'type': self.type,
                'name': self.name,
                'srcs': self.srcs,
            }
            entropy['deps'] = [self.target_database[dep].fingerprint() for dep in self.deps]

            # Add more entropy
            entropy.update(self._fingerprint_entropy())

            # Sort to make the result stable
            entropy_str = str(sorted(entropy.items()))

            # Entropy dict can't cantains normal object, because it's default repr contains address,
            # which is changed in different build, so it should not be used as stable hash entropy.
            # If this assert failed, remove the culprit element from entropy if it is unrelated or
            # override it's `__repe__` if it is related.
            assert ' object at 0x' not in entropy_str
            self.__fingerprint = md5sum(entropy_str)
        return self.__fingerprint

    def _format_message(self, level, msg):
        return '%s: %s: %s: %s' % (self.source_location, level, self.name, msg)

    def debug(self, msg):
        """Print message with target full name prefix"""
        console.debug(self._format_message('debug', msg), prefix=False)

    def info(self, msg):
        """Print message with target full name prefix"""
        console.info(self._format_message('info', msg), prefix=False)

    def warning(self, msg):
        """Print message with target full name prefix"""
        console.warning(self._format_message('warning', msg), prefix=False)

    def error(self, msg):
        """Print message with target full name prefix"""
        console.error(self._format_message('error', msg), prefix=False)

    def fatal(self, msg, code=1):
        """Print message with target full name prefix and exit"""
        # NOTE: VSCode's problem matcher doesn't recognize 'fatal', use 'error' instead
        console.fatal(self._format_message('error', msg), code=code, prefix=False)

    def _prepare_to_generate_rule(self):
        """Should be overridden."""
        self.error('_prepare_to_generate_rule should be overridden in subclasses')

    def _check_name(self):
        if '/' in self.name:
            self.error('Invalid target name, should not contain dir part')

    def _check_kwargs(self, kwargs):
        if kwargs:
            self.error('Unrecognized options %s' % kwargs)

    def _allow_duplicate_source(self):
        """Whether the target allows duplicate source file with other targets"""
        return False

    def _check_sources(self, file_kind, files, exts):
        """Check source files."""
        dups = []
        srcset = set()
        for src in files:
            if src in srcset:
                dups.append(src)
            else:
                srcset.add(src)
            if '..' in src or src.startswith('/'):
                self.error('Invalid %s file path: %s. can only be relative path, and must '
                           'in current directory or subdirectories.' % (file_kind, src))
            if not exts:
                continue
            _, ext = os.path.splitext(src)
            if ext:
                ext = ext[1:]
            if ext not in exts:
                self.error('Invalid %s file name: "%s", must ends with %s' % (file_kind, src, list(exts)))
            full_path = self._source_file_path(src)
            if not os.path.exists(full_path):
                if ext and _is_likely_concatenated_filenames(src, exts):
                    self.warning('File "%s" does not exist, missing "," between file names?' % src)

        if dups:
            self.error('Duplicate %s file paths: %s ' % (file_kind, dups))

    # Keep the relationship of all src -> target.
    # Used by build rules to ensure that a source file occurs in
    # exactly one target(only library target).
    __src_target_map = {}

    def _check_srcs(self, src_exts):
        """Check the "src" attribute."""
        self._check_sources('source', self.srcs, src_exts)
        # Check if one file belongs to two different targets.
        action = config.get_item('global_config', 'duplicated_source_action')
        for src in self.srcs:
            full_src = os.path.normpath(os.path.join(self.path, src))
            target = self.fullname, self._allow_duplicate_source()
            if full_src not in Target.__src_target_map:
                Target.__src_target_map[full_src] = target
            else:
                target_existed = Target.__src_target_map[full_src]
                if target_existed != target:
                    # Always preserve the target which disallows
                    # duplicate source files in the map
                    if target_existed[1]:
                        Target.__src_target_map[full_src] = target
                    elif target[1]:
                        pass
                    else:
                        message = '"%s" is already in srcs of "%s"' % (src, target_existed[0])
                        if action == 'error':
                            self.error(message)
                        elif action == 'warning':
                            self.warning(message)

    def _add_implicit_library(self, implicit_deps):
        """Add implicit dep list to key's deps."""
        for dep in implicit_deps:
            if not dep.startswith('//') and not dep.startswith('#'):
                dep = '//' + dep
            dkey = self._unify_dep(dep)
            if not dkey:
                return
            if dkey[0] == '#':
                self._add_system_library(dkey, dkey[2:])
            if dkey not in self.deps:
                self.deps.append(dkey)
            self._implicit_deps.add(dkey)

    def _add_system_library(self, key, name):
        """Add system library entry to database."""
        if key not in self.target_database:
            assert key[2:] == name
            lib = SystemLibrary(name)
            self.blade.register_target(lib)

    def _add_location_reference_target(self, m):
        """

        Parameters
        -----------
        m: A match object capturing the key and type of the referred target

        Returns
        -----------
        (key, type): the key and type of the referred target

        Description
        -----------
        Location reference makes it possible to refer to the build output of
        another target in the code base.

        General form:
            $(location //path/to:target)

        Some target types may produce more than one output according to the
        build options. Then each output can be referenced by an additional
        type tag:
            $(location //path:name)         # default target output
            $(location //path:name jar)     # jar output
            $(location //path:name so)      # so output

        Note that this method accepts a match object instead of a simple str.
        You could match/search/sub location references in a string with functions
        or RegexObject in re module. For example:

            m = {location regular expression}.search(s)
            if m:
                key, type = self._add_location_reference_target(m)
            else:
                # Not a location reference

        """
        assert m

        key, type = m.groups()
        if not type:
            type = ''
        type = type.strip()
        key = self._unify_dep(key)
        if key and key not in self.deps:
            self.deps.append(key)
        return key, type

    def _unify_dep(self, dep):
        """Unify dep to key."""
        (path, name, msgs) = _parse_target(dep)

        if msgs:
            for msg in msgs:
                self.error('Invalid dependency "%s", ' % dep + msg)
            return None

        if path == '#':
            # System libaray, they don't have entry in BUILD so we need
            # to add deps manually.
            dkey = '#:' + name
            self._add_system_library(dkey, name)
            return dkey

        if path.startswith('//'):
            # Depend on library in remote directory
            path = path[2:]
        else:
            if path:
                # Depend on library in relative subdirectory
                path = os.path.join(self.path, path)
            else:
                # Depend on library in current directory
                path = self.path

        return '%s:%s' % (path, name)

    def _init_target_deps(self, deps):
        """Init the target deps.

        Parameters
        -----------
        deps: the deps list in BUILD file.

        Description
        -----------
        Add target into target database and init the deps list.

        """
        for d in deps:
            dkey = self._unify_dep(d)
            if dkey and dkey not in self.deps:
                self.deps.append(dkey)

    def _init_visibility(self, visibility):
        """Initialize the `visibility` attribute.

        Parameters
        -----------
        visibility: the visibility list in BUILD file

        Description
        -----------
        Visibility determines whether another target is able to depend
        on this target.

        Visibility specify a list of target patterns in the same form as deps,
        i.e. //path:target, '//path/:...'. There is a special value is "PUBLIC",
        which means this target is visible globally within the code base.
        Note that targets inside the same BUILD file are always visible to each
        other.
        """
        if visibility is None:
            global_config = config.get_section('global_config')
            if self.key in global_config.get('legacy_public_targets'):
                visibility = {'PUBLIC'}
            else:
                visibility = global_config.get('default_visibility')
            self._visibility.update(visibility)
            return

        self._visibility_is_default = False
        visibility = var_to_list(visibility)
        if 'PUBLIC' in visibility:
            self._visibility.add('PUBLIC')
            return

        self._visibility.clear()
        for v in visibility:
            if not target_pattern.is_valid_in_build(v):
                #self.error('Invalid build target pattern "%s" for visibility' % v)
                continue
            key = target_pattern.normalize(v, self.path)
            self._visibility.add(key)

    def _match_visibility(self, dep):
        """Check whether the target_id matches dep's visibility."""
        if self.path == dep.path:
            return True
        visibility = dep._visibility
        if 'PUBLIC' in visibility:
            return True
        if self.key in visibility:  # Strict match
            return True
        for pattern in visibility:
            if target_pattern.match(self.key, pattern):
                return True
        return False

    def check_visibility(self):
        """Check whether this target is able to depend on its deps."""
        # Targets are visible inside the same BUILD file by default
        for dep_id in self.deps:
            dep = self.target_database[dep_id]
            if not self._match_visibility(dep):
                self.error('Not allowed to depend on "//%s" because of its visibility,' % dep_id)
                if dep._visibility_is_default:
                    dep.info('No explicit "visibility" declaration, defaults to private, see document for details')
                else:
                    dep.info('which is declared here')

    def _check_deprecated_deps(self):
        """check that whether it depends upon deprecated target.
        It should be overridden in subclass.
        """

    def before_generate(self):  # abstract
        """Will be called before generating build code"""
        assert self.__build_code is None
        self._before_generate()

    def _before_generate(self):  # abstract
        """Will be called before generating build code, overridable"""

    def _expand_deps_generation(self):
        """Expand the generation process and generated rules of dependencies.

        Such as, given a proto_library target, it should generate Java rules
        in addition to C++ rules once it's depended by a java_library target.
        """

    def _get_java_pack_deps(self):
        """
        Return java package dependencies excluding provided dependencies

        target jars represent a path to jar archive. Each jar is built by
        java_library(prebuilt)/scala_library/proto_library.

        maven jars represent maven artifacts within local repository built
        by maven_jar(...).

        Returns:
            A tuple of (target jars, maven jars)
        """
        # TODO(chen3feng): put to `data`
        return [], []

    def _target_dir(self):
        """Return the full path of target dir."""
        return self.target_dir

    def _source_file_path(self, name):
        """Expand the the source file name to full path"""
        return os.path.normpath(os.path.join(self.path, name))

    def _target_file_path(self, file_name):
        """Return the full path of file name in the target dir"""
        return os.path.normpath(os.path.join(self.target_dir, file_name))

    def _remove_build_dir_prefix(self, path):
        """Remove the build dir prefix of path (e.g. build64_release/)
        Args:
            path:str, the full path starts from the workspace root
        """
        prefix = self.build_dir + os.sep
        if path.startswith(prefix):
            return path[len(prefix):]
        return path

    def _add_target_file(self, label, path):
        """
        Parameters
        -----------
        label: label of the target file as key in the dictionary
        path: the path of target file as value in the dictionary

        Description
        -----------
        Keep track of the output files built by the target itself.
        Set the default target if needed.
        """
        self.__targets[label] = path
        if not self.__default_target:
            self.__default_target = path

    def _add_default_target_file(self, label, path):
        """
        Parameters
        -----------
        label: label of the target file as key in the dictionary
        path: the path of target file as value in the dictionary

        Description
        -----------
        Keep track of the default target file which could be referenced
        later without specifying label
        """
        self.__default_target = path
        self._add_target_file(label, path)

    def _get_target_file(self, label=''):
        """
        Parameters
        -----------
        label: label of the file built by the target

        Returns
        -----------
        The target file path or list of file paths

        Description
        -----------
        Return the target file path corresponding to the specified label,
        return empty if label doesn't exist in the dictionary
        """
        # Ensure rules were generated when cached ninja file is used.
        # TODO: _declare_output in __init__
        self.get_build_code()
        if label:
            return self.__targets.get(label, '')
        return self.__default_target

    def _get_target_files(self):
        """
        Returns
        -----------
        All the target files built by the target itself
        """
        self.get_build_code()  # Ensure rules were generated
        results = set()
        for _, v in iteritems(self.__targets):
            if isinstance(v, list):
                results.update(v)
            else:
                results.add(v)
        return sorted(results)

    def _remove_on_clean(self, *paths):
        """Add paths to clean list, to be removed in clean sub command.
        In most cases, you needn't to call this function manually, because in the `generate_build`,
        the outputs will be used to call this function defaultly, unless you need to clean extra
        generated files.
        """
        self.__clean_list += paths

    def get_clean_list(self):
        """Collect paths to be cleaned"""
        return self.__clean_list

    def _write_rule(self, rule):
        """_write_rule.
        Append the rule to the buffer at first.
        Args:
            rule: the rule generated by certain target
        """
        self.__build_code.append('%s\n' % rule)

    def generate(self):
        """Generate build code for specific target."""
        raise NotImplementedError(self.fullname)

    def generate_build(self, rule, outputs, inputs=None,
                       implicit_deps=None, order_only_deps=None,
                       variables=None, implicit_outputs=None, clean=None):
        """Generate a ninja build statement with specified parameters.
        Args:
            clean:list[str], files to be removed on clean, defaults to outputs + implicit_outputs,
                you can pass a empty list to prevent cleaning. (For example, if you want to  remove
                the entire outer dir instead of single files)
            See ninja documents for description for other args.
        """
        outputs = var_to_list(outputs)
        implicit_outputs = var_to_list(implicit_outputs)
        outs = outputs[:]
        if implicit_outputs:
            outs.append('|')
            outs += implicit_outputs
        ins = var_to_list(inputs)
        if implicit_deps:
            ins.append('|')
            ins += var_to_list(implicit_deps)
        if order_only_deps:
            ins.append('||')
            ins += var_to_list(order_only_deps)
        self._write_rule('build %s: %s %s' % (' '.join(outs), rule, ' '.join(ins)))
        clean = (outputs + implicit_outputs) if clean is None else var_to_list(clean)
        if clean:
            self._remove_on_clean(*clean)

        if variables:
            assert isinstance(variables, dict)
            for name, v in iteritems(variables):
                assert v is not None
                if v:
                    self._write_rule('  %s = %s' % (name, v))
                else:
                    self._write_rule('  %s =' % name)
        self._write_rule('')  # An empty line to improve readability

    def get_build_code(self):
        """Return generated build code."""
        # Add a cache to make it idempotent
        if self.__build_code is None:
            self.__build_code = []
            self.generate()
        return self.__build_code


class SystemLibrary(Target):
    def __init__(self, name):
        super(SystemLibrary, self).__init__(
                name=name,
                type='system_library',
                srcs=[],
                src_exts=[],
                deps=[],
                visibility=['PUBLIC'],
                kwargs={})
        self.path = '#'
        self.key = '#:' + name
        self.fullname = '//' + self.key

    def generate(self):
        pass
