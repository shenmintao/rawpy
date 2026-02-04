from setuptools import setup, Extension, find_packages
from setuptools.command.build_ext import build_ext as _build_ext
import subprocess
import errno
import os
import shutil
import sys
import zipfile
import glob
from urllib.request import urlretrieve

import numpy
from Cython.Build import cythonize

# As rawpy is distributed under the MIT license, it cannot use or distribute
# GPL'd code. This is relevant only for the binary wheels which would have to
# bundle the GPL'd code/algorithms (extra demosaic packs).
# Note: RAWPY_BUILD_GPL_CODE=1 only has an effect for macOS and Windows builds
#       because libraw is built from source here, whereas for Linux we look
#       for the library on the system.
# Note: Building GPL demosaic packs only works with libraw <= 0.18.
#       See https://github.com/letmaik/rawpy/issues/72.
buildGPLCode = os.getenv('RAWPY_BUILD_GPL_CODE') == '1'
# don't treat mingw as Windows (https://stackoverflow.com/a/51200002)
isWindows = os.name == 'nt' and 'GCC' not in sys.version
isMac = sys.platform == 'darwin'
isLinux = sys.platform.startswith('linux')
is64Bit = sys.maxsize > 2**32

# adapted from cffi's setup.py
# the following may be overridden if pkg-config exists
libraries = ['libraw_r']
include_dirs = []
library_dirs = []
extra_compile_args = []
extra_link_args = []

if isLinux:
    # rpath=$ORIGIN allows to bundle libraw with the wheel
    extra_link_args += ['-Wl,-rpath,$ORIGIN']
elif isMac:
    # On macOS, rpath is not needed because we set install_name_dir to @loader_path
    # in the cmake build, which makes the library reference @loader_path-relative.
    pass

def _ask_pkg_config(resultlist, option, result_prefix='', sysroot=False):
    pkg_config = os.environ.get('PKG_CONFIG','pkg-config')
    try:
        p = subprocess.Popen([pkg_config, option, 'libraw_r'],
                             stdout=subprocess.PIPE)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise
    else:
        t = p.stdout.read().decode().strip()
        if p.wait() == 0:
            res = t.split()
            # '-I/usr/...' -> '/usr/...'
            for x in res:
                assert x.startswith(result_prefix)
            res = [x[len(result_prefix):] for x in res]

            sysroot = sysroot and os.environ.get('PKG_CONFIG_SYSROOT_DIR', '')
            if sysroot:
                # old versions of pkg-config don't support this env var,
                # so here we emulate its effect if needed
                res = [path if path.startswith(sysroot)
                            else sysroot + path
                         for path in res]
            resultlist[:] = res

# Some thoughts on bundling LibRaw in Linux installs:
# Compiling and bundling libraw.so like in the Windows wheels is likely not
# easily possible for Linux. This is due to the fact that the dynamic linker ld
# doesn't search for libraw.so in the directory where the Python extension is in.
# The -rpath with $ORIGIN method can not be used in this case as $ORIGIN is always
# relative to the executable and not the shared library,
# see https://stackoverflow.com/q/6323603.
# But note that this was never tested and may actually still work somehow.
# matplotlib works around such problems by including external libraries as pure
# Python extensions, partly rewriting their sources and removing any dependency
# on a configure script, or cmake or other build infrastructure.
# A possible work-around could be to statically link against libraw.

external_dir = os.path.abspath('external')
libraw_dir = os.path.join(external_dir, 'LibRaw')
cmake_build = os.path.join(external_dir, 'LibRaw-cmake', 'build')
install_dir = os.path.join(cmake_build, 'install')

include_dirs += [os.path.join(install_dir, 'include', 'libraw')]
library_dirs += [os.path.join(install_dir, 'lib')]
libraries = ['raw_r']

# we use cmake, so libraw_config.h will always exist
libraw_config_found = True

define_macros = [('_HAS_LIBRAW_CONFIG_H', '1' if libraw_config_found else '0')]

if isWindows:
    extra_compile_args += ['/DWIN32']
    
# this must be after use_pkg_config()!
include_dirs += [numpy.get_include()]

def clone_submodules():
    if not os.path.exists('external/LibRaw/README.md'):
        print('LibRaw git submodule is not cloned yet, will invoke "git submodule update --init" now')
        if os.system('git submodule update --init') != 0:
            raise Exception('git failed')
        
def windows_libraw_compile():
    clone_submodules()
    
    # download cmake to compile libraw
    # the cmake zip contains a cmake-3.12.4-win32-x86 folder when extracted
    cmake_url = 'https://cmake.org/files/v3.12/cmake-3.12.4-win32-x86.zip'
    cmake = os.path.abspath('external/cmake-3.12.4-win32-x86/bin/cmake.exe')
    
    files = [(cmake_url, 'external', cmake)]
    
    for url, extractdir, extractcheck in files:
        if not os.path.exists(extractcheck):
            path = 'external/' + os.path.basename(url)
            if not os.path.exists(path):
                print('Downloading', url)
                try:
                    urlretrieve(url, path)
                except:
                    # repeat once in case of network issues
                    urlretrieve(url, path)
            
            with zipfile.ZipFile(path) as z:
                print('Extracting', path, 'into', extractdir)
                z.extractall(extractdir)
            
            if not os.path.exists(path):
                raise RuntimeError(path + ' not found!')
                
    # openmp dll
    # VS 2017 and higher
    vc_redist_dir = os.getenv('VCToolsRedistDir')
    vs_target_arch = os.getenv('VSCMD_ARG_TGT_ARCH')
    if not vc_redist_dir:
        # VS 2015
        vc_redist_dir = os.path.join(os.environ['VCINSTALLDIR'], 'redist')            
        vs_target_arch = 'x64' if is64Bit else 'x86'
        
    omp_glob = os.path.join(vc_redist_dir, vs_target_arch, 'Microsoft.VC*.OpenMP', 'vcomp*.dll')
    omp_dlls = glob.glob(omp_glob)

    if len(omp_dlls) == 1:
        has_openmp_dll = True
        omp = omp_dlls[0]
    elif len(omp_dlls) > 1:
        print('WARNING: disabling OpenMP because multiple runtime DLLs were found:')
        for omp_dll in omp_dlls:
            print(omp_dll)
        has_openmp_dll = False
    else:
        print('WARNING: disabling OpenMP because no runtime DLLs were found')
        has_openmp_dll = False
    
    # configure and compile libraw
    cwd = os.getcwd()
    shutil.rmtree(cmake_build, ignore_errors=True)
    os.makedirs(cmake_build, exist_ok=True)
    os.chdir(cmake_build)
        
    # Important: always use Release build type, otherwise the library will depend on a
    #            debug version of OpenMP which is not what we bundle it with, and then it would fail
    enable_openmp_flag = 'ON' if has_openmp_dll else 'OFF'
    cmds = [cmake + ' .. -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release ' +\
                    '-DCMAKE_PREFIX_PATH=' + os.environ['CMAKE_PREFIX_PATH'] + ' ' +\
                    '-DLIBRAW_PATH=' + libraw_dir.replace('\\', '/') + ' ' +\
                    '-DENABLE_X3FTOOLS=ON -DENABLE_6BY9RPI=ON ' +\
                    '-DENABLE_EXAMPLES=OFF -DENABLE_OPENMP=' + enable_openmp_flag + ' -DENABLE_RAWSPEED=OFF ' +\
                    ('-DENABLE_DEMOSAIC_PACK_GPL2=ON -DDEMOSAIC_PACK_GPL2_RPATH=../../LibRaw-demosaic-pack-GPL2 ' +\
                     '-DENABLE_DEMOSAIC_PACK_GPL3=ON -DDEMOSAIC_PACK_GPL3_RPATH=../../LibRaw-demosaic-pack-GPL3 '
                     if buildGPLCode else '') +\
                    '-DCMAKE_INSTALL_PREFIX=install',
            cmake + ' --build . --target install',
            ]
    for cmd in cmds:
        print(cmd)
        code = os.system(cmd)
        if code != 0:
            sys.exit(code)
    os.chdir(cwd)
    
    # bundle runtime dlls
    dll_runtime_libs = [('raw_r.dll', os.path.join(install_dir, 'bin'))]
    
    if has_openmp_dll:
        # Check if OpenMP was enabled in the CMake build, independent of the flag we supplied.
        # If not, we don't have to bundle the DLL.
        libraw_configh = os.path.join(install_dir, 'include', 'libraw', 'libraw_config.h')
        match = '#define LIBRAW_USE_OPENMP 1'
        has_openmp_support = match in open(libraw_configh).read()
        if has_openmp_support:
            dll_runtime_libs.append((os.path.basename(omp), os.path.dirname(omp)))
        else:
            print('WARNING: "#define LIBRAW_USE_OPENMP 1" not found even though OpenMP was enabled')
            print('Will not bundle OpenMP runtime DLL')
 
    for filename, folder in dll_runtime_libs:
        src = os.path.join(folder, filename)
        dest = 'rawpy/' + filename
        print('copying', src, '->', dest)
        shutil.copyfile(src, dest)
 
def unix_libraw_compile():
    clone_submodules()
        
    # configure and compile libraw
    cwd = os.getcwd()
    shutil.rmtree(cmake_build, ignore_errors=True)
    os.makedirs(cmake_build, exist_ok=True)
    os.chdir(cmake_build)
        
    cmake_cmd = 'cmake .. -DCMAKE_BUILD_TYPE=Release ' +\
                    '-DLIBRAW_PATH=' + libraw_dir + ' ' +\
                    '-DENABLE_X3FTOOLS=ON -DENABLE_6BY9RPI=ON ' +\
                    '-DENABLE_OPENMP=OFF ' +\
                    '-DENABLE_EXAMPLES=OFF -DENABLE_RAWSPEED=OFF ' +\
                    ('-DENABLE_DEMOSAIC_PACK_GPL2=ON -DDEMOSAIC_PACK_GPL2_RPATH=../../LibRaw-demosaic-pack-GPL2 ' +\
                     '-DENABLE_DEMOSAIC_PACK_GPL3=ON -DDEMOSAIC_PACK_GPL3_RPATH=../../LibRaw-demosaic-pack-GPL3 '
                     if buildGPLCode else '') +\
                    '-DCMAKE_INSTALL_PREFIX=install'
    
    if isMac:
        # This sets the install_name of the dylib to @loader_path/...
        # so that the extension module can find it even if it's bundled in a wheel.
        cmake_cmd += ' -DCMAKE_INSTALL_NAME_DIR=@loader_path'
    
    cmds = [cmake_cmd,
            'cmake --build . --target install',
            ]
    for cmd in cmds:
        print(cmd)
        code = os.system(cmd)
        if code != 0:
            sys.exit(code)
    os.chdir(cwd)
        
package_data = {}

# evil hack, check cmd line for relevant commands
# custom cmdclasses didn't work out in this case
cmdline = ''.join(sys.argv[1:])
class build_ext(_build_ext):
    def run(self):
        if isWindows:
            windows_libraw_compile()
        elif isMac or isLinux:
            unix_libraw_compile()
        
        super().run()
        
        dest_dir = os.path.join(self.build_lib, 'rawpy')
        if isWindows:
            libs = glob.glob(os.path.join(install_dir, 'bin', 'raw_r.dll'))
            libs += glob.glob(os.path.join(install_dir, 'bin', 'vcomp*.dll'))
            if not libs:
                raise Exception('no libraw binaries found after compilation')
            for lib_path in libs:
                print('copying', lib_path, '->', dest_dir)
                shutil.copy(lib_path, dest_dir)
        elif isMac or isLinux:
            if isMac:
                lib_dir = os.path.join(install_dir, 'lib')
                if not os.path.isdir(lib_dir):
                    raise Exception(f'lib directory not found in {install_dir}')

                libs = glob.glob(os.path.join(lib_dir, 'libraw_r*.dylib'))
                if not libs:
                    raise Exception(f'libraw_r.dylib* not found after compilation in {lib_dir}')
                
                for lib_path in libs:
                    dest_path = os.path.join(dest_dir, os.path.basename(lib_path))
                    print(f'copying {lib_path} -> {dest_path}')
                    shutil.copy(lib_path, dest_path, follow_symlinks=True)
            elif isLinux:
                lib_dir = os.path.join(install_dir, 'lib')
                if not os.path.isdir(lib_dir):
                    lib_dir = os.path.join(install_dir, 'lib64') # try lib64
                if not os.path.isdir(lib_dir):
                    raise Exception(f'lib directory not found in {install_dir}')

                libs = glob.glob(os.path.join(lib_dir, 'libraw_r.so*'))
                if not libs:
                    raise Exception(f'libraw_r.so* not found after compilation in {lib_dir}')
                
                for lib_path in libs:
                    dest_path = os.path.join(dest_dir, os.path.basename(lib_path))
                    print(f'copying {lib_path} -> {dest_path}')
                    shutil.copy(lib_path, dest_path, follow_symlinks=True)

if any(s in cmdline for s in ['clean', 'sdist']):
    # When running sdist after a previous run of bdist or build_ext
    # then even with the 'clean' command the .egg-info folder stays.
    # This folder contains SOURCES.txt which in turn is used by sdist
    # to include package data files, but we don't want .dll's and .xml
    # files in our source distribution. Therefore, to prevent accidents,
    # we help a little...
    egg_info = 'rawpy.egg-info'
    print('removing', egg_info)
    shutil.rmtree(egg_info, ignore_errors=True)

extensions = cythonize([Extension("rawpy._rawpy",
              include_dirs=include_dirs,
              sources=[os.path.join('rawpy', '_rawpy.pyx')],
              libraries=libraries,
              library_dirs=library_dirs,
              define_macros=define_macros,
              extra_compile_args=extra_compile_args,
              extra_link_args=extra_link_args,
             )])

# make __version__ available (https://stackoverflow.com/a/16084844)
exec(open('rawpy/_version.py').read())

setup(
      name = 'rawpy',
      version = __version__,
      description = 'RAW image processing for Python, a wrapper for libraw',
      long_description = open('README.md').read(),
      long_description_content_type='text/markdown',
      author = 'Maik Riechert',
      url = 'https://github.com/letmaik/rawpy',
      classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Natural Language :: English',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Cython',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
        'Operating System :: MacOS',
        'Operating System :: Microsoft :: Windows',
        'Operating System :: POSIX',
        'Operating System :: Unix',
        'Topic :: Multimedia :: Graphics',
        'Topic :: Software Development :: Libraries',
      ],
      packages = find_packages(),
      ext_modules = extensions,
      cmdclass={'build_ext': build_ext},
      install_requires=['numpy >= 1.26.0']
)
