import subprocess
import logging
import os
import urllib.parse
import zipfile
import tarfile
import shutil
import platform
import argparse
import multiprocessing
import hashlib
from typing import Callable, NamedTuple, Optional, List, Union, Dict
if platform.system() == 'Windows':
    import winreg


logging.basicConfig(level=logging.DEBUG)


class ChangeDirectory(object):
    def __init__(self, cwd):
        self._cwd = cwd

    def __enter__(self):
        self._old_cwd = os.getcwd()
        logging.debug(f'pushd {self._old_cwd} --> {self._cwd}')
        os.chdir(self._cwd)

    def __exit__(self, exctype, excvalue, trace):
        logging.debug(f'popd {self._old_cwd} <-- {self._cwd}')
        os.chdir(self._old_cwd)
        return False


def cd(cwd):
    return ChangeDirectory(cwd)


def cmd(args, **kwargs):
    logging.debug(f'+{args} {kwargs}')
    if 'check' not in kwargs:
        kwargs['check'] = True
    if 'resolve' in kwargs:
        resolve = kwargs['resolve']
        del kwargs['resolve']
    else:
        resolve = True
    if resolve:
        args = [shutil.which(args[0]), *args[1:]]
    return subprocess.run(args, **kwargs)


# 標準出力をキャプチャするコマンド実行。シェルの `cmd ...` や $(cmd ...) と同じ
def cmdcap(args, **kwargs):
    # 3.7 でしか使えない
    # kwargs['capture_output'] = True
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE
    kwargs['encoding'] = 'utf-8'
    return cmd(args, **kwargs).stdout.strip()


# https://stackoverflow.com/a/2656405
def onerror(func, path, exc_info):
    """
    Error handler for ``shutil.rmtree``.

    If the error is due to an access error (read only file)
    it attempts to add write permission and then retries.

    If the error is for another reason it re-raises the error.
    
    Usage : ``shutil.rmtree(path, onerror=onerror)``
    """
    import stat
    # Is the error an access error?
    if not os.access(path, os.W_OK):
        os.chmod(path, stat.S_IWUSR)
        func(path)
    else:
        raise


def rm_rf(path: str):
    if not os.path.exists(path):
        logging.debug(f'rm -rf {path} => path not found')
        return
    if os.path.isfile(path) or os.path.islink(path):
        os.remove(path)
        logging.debug(f'rm -rf {path} => file removed')
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)
        logging.debug(f'rm -rf {path} => directory removed')


def mkdir_p(path: str):
    if os.path.exists(path):
        logging.debug(f'mkdir -p {path} => already exists')
        return
    os.makedirs(path, exist_ok=True)
    logging.debug(f'mkdir -p {path} => directory created')


if platform.system() == 'Windows':
    PATH_SEPARATOR = ';'
else:
    PATH_SEPARATOR = ':'


def add_path(path: str, is_after=False):
    logging.debug(f'add_path: {path}')
    if 'PATH' not in os.environ:
        os.environ['PATH'] = path
        return

    if is_after:
        os.environ['PATH'] = os.environ['PATH'] + PATH_SEPARATOR + path
    else:
        os.environ['PATH'] = path + PATH_SEPARATOR + os.environ['PATH']


def download(url: str, output_dir: Optional[str] = None, filename: Optional[str] = None) -> str:
    if filename is None:
        output_path = urllib.parse.urlparse(url).path.split('/')[-1]
    else:
        output_path = filename

    if output_dir is not None:
        output_path = os.path.join(output_dir, output_path)

    if os.path.exists(output_path):
        return output_path

    try:
        if shutil.which('curl') is not None:
            cmd(["curl", "-fLo", output_path, url])
        else:
            cmd(["wget", "-cO", output_path, url])
    except Exception:
        # ゴミを残さないようにする
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    return output_path


def read_version_file(path: str) -> Dict[str, str]:
    versions = {}

    lines = open(path).readlines()
    for line in lines:
        line = line.strip()

        # コメント行
        if line[:1] == '#':
            continue

        # 空行
        if len(line) == 0:
            continue

        [a, b] = map(lambda x: x.strip(), line.split('=', 2))
        versions[a] = b.strip('"')

    return versions


# dir 以下にある全てのファイルパスを、dir2 からの相対パスで返す
def enum_all_files(dir, dir2):
    for root, _, files in os.walk(dir):
        for file in files:
            yield os.path.relpath(os.path.join(root, file), dir2)


def versioned(func):
    def wrapper(version, version_file, *args, **kwargs):
        if 'ignore_version' in kwargs:
            if kwargs.get('ignore_version'):
                rm_rf(version_file)
            del kwargs['ignore_version']

        if os.path.exists(version_file):
            ver = open(version_file).read()
            if ver.strip() == version.strip():
                return

        r = func(version=version, *args, **kwargs)

        with open(version_file, 'w') as f:
            f.write(version)

        return r

    return wrapper


# アーカイブが単一のディレクトリに全て格納されているかどうかを調べる。
#
# 単一のディレクトリに格納されている場合はそのディレクトリ名を返す。
# そうでない場合は None を返す。
def _is_single_dir(infos: List[Union[zipfile.ZipInfo, tarfile.TarInfo]],
                   get_name: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], str],
                   is_dir: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], bool]) -> Optional[str]:
    # tarfile: ['path', 'path/to', 'path/to/file.txt']
    # zipfile: ['path/', 'path/to/', 'path/to/file.txt']
    # どちらも / 区切りだが、ディレクトリの場合、後ろに / が付くかどうかが違う
    dirname = None
    for info in infos:
        name = get_name(info)
        n = name.rstrip('/').find('/')
        if n == -1:
            # ルートディレクトリにファイルが存在している
            if not is_dir(info):
                return None
            dir = name.rstrip('/')
        else:
            dir = name[0:n]
        # ルートディレクトリに２個以上のディレクトリが存在している
        if dirname is not None and dirname != dir:
            return None
        dirname = dir

    return dirname


def is_single_dir_tar(tar: tarfile.TarFile) -> Optional[str]:
    return _is_single_dir(tar.getmembers(), lambda t: t.name, lambda t: t.isdir())


def is_single_dir_zip(zip: zipfile.ZipFile) -> Optional[str]:
    return _is_single_dir(zip.infolist(), lambda z: z.filename, lambda z: z.is_dir())


# 解凍した上でファイル属性を付与する
def _extractzip(z: zipfile.ZipFile, path: str):
    z.extractall(path)
    if platform.system() == 'Windows':
        return
    for info in z.infolist():
        if info.is_dir():
            continue
        filepath = os.path.join(path, info.filename)
        mod = info.external_attr >> 16
        if (mod & 0o120000) == 0o120000:
            # シンボリックリンク
            with open(filepath, 'r') as f:
                src = f.read()
            os.remove(filepath)
            with cd(os.path.dirname(filepath)):
                if os.path.exists(src):
                    os.symlink(src, filepath)
        if os.path.exists(filepath):
            # 普通のファイル
            os.chmod(filepath, mod & 0o777)


# zip または tar.gz ファイルを展開する。
#
# 展開先のディレクトリは {output_dir}/{output_dirname} となり、
# 展開先のディレクトリが既に存在していた場合は削除される。
#
# もしアーカイブの内容が単一のディレクトリであった場合、
# そのディレクトリは無いものとして展開される。
#
# つまりアーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/file1
# - out/libsora/file2
# が出力される。
#
# また、アーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2', 'LICENSE']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/libsora-1.23/file1
# - out/libsora/libsora-1.23/file2
# - out/libsora/LICENSE
# が出力される。
def extract(file: str, output_dir: str, output_dirname: str, filetype: Optional[str] = None):
    path = os.path.join(output_dir, output_dirname)
    logging.info(f"Extract {file} to {path}")
    if filetype == 'gzip' or file.endswith('.tar.gz'):
        rm_rf(path)
        with tarfile.open(file) as t:
            dir = is_single_dir_tar(t)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                t.extractall(path)
            else:
                logging.info(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                t.extractall(output_dir)
                if path != path2:
                    logging.debug(f"mv {path2} {path}")
                    os.replace(path2, path)
    elif filetype == 'zip' or file.endswith('.zip'):
        rm_rf(path)
        with zipfile.ZipFile(file) as z:
            dir = is_single_dir_zip(z)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                # z.extractall(path)
                _extractzip(z, path)
            else:
                logging.info(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                # z.extractall(output_dir)
                _extractzip(z, output_dir)
                if path != path2:
                    logging.debug(f"mv {path2} {path}")
                    os.replace(path2, path)
    else:
        raise Exception('file should end with .tar.gz or .zip')


def clone_and_checkout(url, version, dir, fetch, fetch_force):
    if fetch_force:
        rm_rf(dir)

    if not os.path.exists(os.path.join(dir, '.git')):
        cmd(['git', 'clone', url, dir])
        fetch = True

    if fetch:
        with cd(dir):
            cmd(['git', 'fetch'])
            cmd(['git', 'reset', '--hard'])
            cmd(['git', 'clean', '-df'])
            cmd(['git', 'checkout', '-f', version])


def git_clone_shallow(url, hash, dir):
    rm_rf(dir)
    mkdir_p(dir)
    with cd(dir):
        cmd(['git', 'init'])
        cmd(['git', 'remote', 'add', 'origin', url])
        cmd(['git', 'fetch', '--depth=1', 'origin', hash])
        cmd(['git', 'reset', '--hard', 'FETCH_HEAD'])


def cmake_path(path: str) -> str:
    return path.replace('\\', '/')


@versioned
def install_cmake(version, source_dir, install_dir, platform: str, ext):
    url = f'https://github.com/Kitware/CMake/releases/download/v{version}/cmake-{version}-{platform}.{ext}'
    path = download(url, source_dir)
    extract(path, install_dir, 'cmake')


@versioned
def install_android_ndk(version, install_dir, source_dir):
    archive = download(
        f'https://dl.google.com/android/repository/android-ndk-{version}-linux.zip',
        source_dir)
    rm_rf(os.path.join(install_dir, 'android-ndk'))
    extract(archive, output_dir=install_dir, output_dirname='android-ndk')


BASE_DIR = os.path.abspath(os.path.dirname(__file__))


AVAILABLE_TARGETS = ['windows_x86_64', 'macos_x86_64', 'macos_arm64', 'ubuntu-20.04_x86_64',
                     'ios', 'android']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=AVAILABLE_TARGETS)
    parser.add_argument("--debug", action='store_true')
    parser.add_argument("--relwithdebinfo", action='store_true')
    parser.add_argument("--package", action='store_true')

    args = parser.parse_args()
    platform = args.target

    configuration = 'debug' if args.debug else 'release'
    source_dir = os.path.join(BASE_DIR, '_source', platform, configuration)
    build_dir = os.path.join(BASE_DIR, '_build', platform, configuration)
    install_dir = os.path.join(BASE_DIR, '_install', platform, configuration)
    package_dir = os.path.join(BASE_DIR, '_package', platform, configuration)
    mkdir_p(source_dir)
    mkdir_p(build_dir)
    mkdir_p(install_dir)

    with cd(BASE_DIR):
        version = read_version_file('VERSION')
        cmake_version = version['CMAKE_VERSION']
        msquic_version = version['MSQUIC_VERSION']

    # CMake
    install_cmake_args = {
        'version': cmake_version,
        'version_file': os.path.join(install_dir, 'cmake.version'),
        'source_dir': source_dir,
        'install_dir': install_dir,
        'platform': '',
        'ext': 'tar.gz'
    }
    if platform in ('windows_x86_64', 'windows_arm64'):
        install_cmake_args['platform'] = 'windows-x86_64'
        install_cmake_args['ext'] = 'zip'
    elif platform in ('macos_x86_64', 'macos_arm64', 'ios'):
        install_cmake_args['platform'] = 'macos-universal'
    elif platform in ('ubuntu-20.04_x86_64', 'ubuntu-20.04_arm64', 'android'):
        install_cmake_args['platform'] = 'linux-x86_64'
    else:
        raise Exception('Failed to install CMake')
    install_cmake(**install_cmake_args)

    if platform in ('macos_x86_64', 'macos_arm64', 'ios'):
        add_path(os.path.join(install_dir, 'cmake', 'CMake.app', 'Contents', 'bin'))
    else:
        add_path(os.path.join(install_dir, 'cmake', 'bin'))

    # Android NDK
    if platform == 'android':
        install_android_ndk_args = {
            'version': version['ANDROID_NDK_VERSION'],
            'version_file': os.path.join(install_dir, 'android-ndk.version'),
            'source_dir': source_dir,
            'install_dir': install_dir,
        }
        install_android_ndk(**install_android_ndk_args)
        add_path(os.path.join(install_dir, 'android-ndk', 'toolchains', 'llvm', 'prebuilt', 'linux-x86_64', 'bin'))
        os.environ['ANDROID_NDK_HOME'] = os.path.join(install_dir, 'android-ndk')

    configuration = 'Release'
    if args.debug:
        configuration = 'Debug'
    if args.relwithdebinfo:
        configuration = 'RelWithDebInfo'

    msquic_source_dir = os.path.join(source_dir, 'msquic')
    msquic_build_dir = os.path.join(build_dir, 'msquic')
    msquic_install_dir = os.path.join(install_dir, 'msquic')
    rm_rf(msquic_build_dir)
    mkdir_p(msquic_build_dir)

    git_clone_shallow('https://github.com/microsoft/msquic.git', msquic_version, msquic_source_dir)
    with cd(msquic_source_dir):
        # 以下のような形式で出力されるので、それぞれパスとハッシュ値に分ける
        # -a4f472c5fe2c8298c0ada2e24717458c45a17eb1 submodules/clog
        # -dd7a9d29a33de34836c345c3b753d4eba15c5f44 submodules/googletest
        # -6d6e737a473eba179ea9b666a7bc2e3873c1c5c7 submodules/openssl
        r = cmdcap(['git', 'submodule', 'status'])
        xs = map(lambda line: line[1:].split(), r.splitlines())
        # ハッシュ値のコミットだけ clone してくる
        for [hash, path] in xs:
            # .gitmodules から URL を取得
            url = cmdcap(['git', 'config', '-f', '.gitmodules', f'submodule.{path}.url'])
            git_clone_shallow(url, hash, path)

    with cd(msquic_build_dir):
        cmake_args = []
        cmake_args.append(f'-DCMAKE_BUILD_TYPE={configuration}')
        cmake_args.append(f"-DCMAKE_INSTALL_PREFIX={cmake_path(os.path.join(install_dir, 'msquic'))}")
        cmake_args.append('-DQUIC_BUILD_SHARED=OFF')
        if platform in ('macos_x86_64', 'macos_arm64'):
            sysroot = cmdcap(['xcrun', '--sdk', 'macosx', '--show-sdk-path'])
            arch = 'x86_64' if platform == 'macos_x86_64' else 'arm64'
            target = 'x86_64-apple-darwin' if platform == 'macos_x86_64' else 'aarch64-apple-darwin'
            cmake_args.append(f'-DCMAKE_SYSTEM_PROCESSOR={arch}')
            cmake_args.append(f'-DCMAKE_OSX_ARCHITECTURES={arch}')
            cmake_args.append(f'-DCMAKE_C_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_CXX_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_OBJCXX_COMPILER_TARGET={target}')
            cmake_args.append(f'-DCMAKE_SYSROOT={sysroot}')
        if platform == 'ios':
            cmake_args += ['-G', 'Xcode']
            cmake_args.append(f"-DCMAKE_TOOLCHAIN_FILE={cmake_path(os.path.join(msquic_source_dir, 'cmake', 'toolchains', 'ios.cmake'))}")
            cmake_args.append('-DDEPLOYMENT_TARGET=13.0')
            cmake_args.append('-DENABLE_ARC=0')
            cmake_args.append('-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0')
            # cmake_args.append("-DCMAKE_SYSTEM_NAME=iOS")
            # cmake_args.append("-DCMAKE_OSX_ARCHITECTURES=x86_64;arm64")
            # cmake_args.append("-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0")
            # cmake_args.append("-DCMAKE_XCODE_ATTRIBUTE_ONLY_ACTIVE_ARCH=NO")
            # 以下のエラーが出てしまうのを何とかする
            # /path/to/msquic-build/_source/ios/release/msquic/src/core/frame.c:1841:38: error: implicit conversion loses integer precision: 'QUIC_VAR_INT'
            #      (aka 'unsigned long long') to 'QUIC_FRAME_TYPE' (aka 'enum QUIC_FRAME_TYPE') [-Werror,-Wshorten-64-to-32]
            cmake_args.append("-DCMAKE_C_FLAGS=-Wno-shorten-64-to-32")
        if platform == 'android':
            cmake_args.append('-DANDROID_ABI=arm64-v8a')
            cmake_args.append('-DANDROID_PLATFORM=android-29')
            ndk_home = os.path.join(install_dir, 'android-ndk')
            cmake_args.append(f"-DANDROID_NDK={cmake_path(ndk_home)}")
            cmake_args.append(f"-DCMAKE_TOOLCHAIN_FILE={cmake_path(os.path.join(ndk_home, 'build', 'cmake', 'android.toolchain.cmake'))}")

        if platform == 'ios':
            mkdir_p('x86_64')
            with cd('x86_64'):
                cmd(['cmake', msquic_source_dir, *cmake_args, '-DPLATFORM=SIMULATOR64'])
                cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration,
                    '--target', 'msquic_lib'])
            mkdir_p('arm64')
            with cd('arm64'):
                cmd(['cmake', msquic_source_dir, *cmake_args, '-DPLATFORM=OS64'])
                cmd(['cmake', '--build', '.', f'-j{multiprocessing.cpu_count()}', '--config', configuration,
                    '--target', 'msquic_lib'])
                # 後でライブラリは差し替えるけど、他のデータをコピーするためにとりあえず install は呼んでおく
                cmd(['cmake', '--install', '.'])
            cmd(['lipo', '-create', '-output', os.path.join(msquic_build_dir, 'libmsquic.a'),
                os.path.join(msquic_build_dir, 'x86_64', 'bin', f'{configuration}', 'libmsquic.a'),
                os.path.join(msquic_build_dir, 'arm64', 'bin', f'{configuration}', 'libmsquic.a')])
            mkdir_p(os.path.join(msquic_install_dir, 'lib'))
            shutil.copyfile(os.path.join(msquic_build_dir, 'libmsquic.a'),
                            os.path.join(msquic_install_dir, 'lib', 'libmsquic.a'))
        else:
            cmd(['cmake', msquic_source_dir, *cmake_args])
            cmd(['cmake', '--build', '.', '--target', 'msquic_lib', f'-j{multiprocessing.cpu_count()}', '--config', configuration])
            cmd(['cmake', '--install', '.', '--config', configuration])
            if platform in ('windows_x86_64', 'windows_arm64'):
                msquic_lib = 'msquic.lib'
            elif platform in ('ubuntu-20.04_x86_64', 'macos_x86_64', 'macos_arm64', 'android', 'ios'):
                msquic_lib = 'libmsquic.a'
            # msquic.lib がインストールされてないのでちゃんとコピーする
            mkdir_p(os.path.join(msquic_install_dir, 'lib'))
            shutil.copyfile(os.path.join(msquic_build_dir, 'bin', 'Release', msquic_lib),
                            os.path.join(msquic_install_dir, 'lib', msquic_lib))

    if args.package:
        rm_rf(package_dir)
        mkdir_p(package_dir)
        with cd(install_dir):
            # msquic のライセンスファイルを同封する
            shutil.copyfile(os.path.join(msquic_source_dir, 'LICENSE'),
                            os.path.join(msquic_install_dir, 'LICENSE'))
            if platform in ('windows_x86_64', 'windows_arm64'):
                archive_name = f'msquic-{msquic_version}_{platform}.zip'
                archive_path = os.path.join(package_dir, archive_name)
                with zipfile.ZipFile(archive_path, 'w') as f:
                    for file in enum_all_files('msquic', '.'):
                        f.write(filename=file, arcname=file)
                with open(os.path.join(package_dir, 'msquic.env'), 'w') as f:
                    f.write('CONTENT_TYPE=application/zip\n')
                    f.write(f'PACKAGE_NAME={archive_name}\n')
            else:
                archive_name = f'msquic-{msquic_version}_{platform}.tar.gz'
                archive_path = os.path.join(package_dir, archive_name)
                with tarfile.open(archive_path, 'w:gz') as f:
                    for file in enum_all_files('msquic', '.'):
                        f.add(name=file, arcname=file)
                with open(os.path.join(package_dir, 'msquic.env'), 'w') as f:
                    f.write("CONTENT_TYPE=application/gzip\n")
                    f.write(f'PACKAGE_NAME={archive_name}\n')


if __name__ == '__main__':
    main()
