from itertools import product
from typing import Optional

import xml.etree.ElementTree as ET

import pytest
from pytest_container import DerivedContainer
from pytest_container.container import ContainerData


_RPMS_DIR = "/src/rpms/"

_AAA_BASE_URL = "https://github.com/openSUSE/aaa_base"
_LIBECONF_URL = "https://github.com/openSUSE/libeconf"

CONTAINERFILE = f"""RUN set -eux; \
    zypper -n in python311 git-core diff python311-PyYAML; \
    . /etc/os-release && [[ ${{NAME}} = "SLES" ]] || zypper -n in git-lfs; \
    zypper -n in -f --recommends build; \
    zypper -n clean; rm -rf /var/log/zypp*

RUN git config --global user.name "SUSE Bot" && \
    git config --global user.email "noreply@suse.com" && \
    git config --global protocol.file.allow always

RUN mkdir -p {_RPMS_DIR}ring0 && \
    cd {_RPMS_DIR} && git clone {_LIBECONF_URL} && \
    cd libeconf && git rev-parse HEAD > /src/libeconf && \
    cd {_RPMS_DIR}ring0 && \
    git init && git submodule add {_AAA_BASE_URL} && \
    git commit -m "add aaa_base" && \
    git submodule add ../libeconf && git commit -m "add libeconf" && \
    cd aaa_base && git rev-parse HEAD > /src/aaa_base

RUN set -euo pipefail; \
    mkdir -p {_RPMS_DIR}proj; pushd {_RPMS_DIR}proj; \
    git init; \
    cp -r {_RPMS_DIR}ring0/libeconf .; rm -rf libeconf/.git; \
    cp -r {_RPMS_DIR}ring0/aaa_base .; rm -rf aaa_base/.git; \
    git add libeconf aaa_base; git commit -m "initial commit";

COPY obs_scm_bridge /usr/bin/
RUN sed -i 's,^#!/usr/bin/python3.*,#!/usr/bin/python3.11,' /usr/bin/obs_scm_bridge
RUN chmod +x /usr/bin/obs_scm_bridge
"""

EXTRA_ENVIRONMENT_VARIABLES = {
    'SCM_BRIDGE_TESTCASE': '1'
}

TUMBLEWEED = DerivedContainer(
    base="registry.opensuse.org/opensuse/tumbleweed", containerfile=CONTAINERFILE,
    extra_environment_variables=EXTRA_ENVIRONMENT_VARIABLES
)
LEAP_LATEST = DerivedContainer(
    base="registry.opensuse.org/opensuse/leap:latest",
    containerfile=CONTAINERFILE,
    extra_environment_variables=EXTRA_ENVIRONMENT_VARIABLES
)

BCI_BASE_LATEST = DerivedContainer(
    base="registry.suse.com/bci/bci-base:latest", containerfile=CONTAINERFILE,
    extra_environment_variables=EXTRA_ENVIRONMENT_VARIABLES
)

CONTAINER_IMAGES = [TUMBLEWEED, LEAP_LATEST, BCI_BASE_LATEST]


_OBS_SCM_BRIDGE_CMD = "obs_scm_bridge --debug 1"


def test_service_help(auto_container: ContainerData):
    """This is just a simple smoke test to check whether the script works."""
    auto_container.connection.run_expect([0], f"{_OBS_SCM_BRIDGE_CMD} --help")


def test_clones_the_repository(auto_container_per_test: ContainerData):
    """Check that the service clones the manually created repository correctly."""
    dest = "/tmp/ring0"
    auto_container_per_test.connection.run_expect(
            [0], f"{_OBS_SCM_BRIDGE_CMD} --outdir {dest} --url file://{_RPMS_DIR}ring0"
    )
    auto_container_per_test.connection.run_expect([0], f"diff {dest} {_RPMS_DIR}ring0")


@pytest.mark.parametrize("container_per_test", CONTAINER_IMAGES, indirect=True)
@pytest.mark.parametrize(
    "fragment",
    (
        "",
        "#main",
        # sha of the 0.3.0 release tag
        "#9907826c17ca7b650c4040e9c2b45bfef4d9821f",
    ),
)
def test_clones_subdir(container_per_test: ContainerData, fragment: str):
    dest = "/tmp/scm-bridge/"
    container_per_test.connection.run_expect(
        [0],
        f"{_OBS_SCM_BRIDGE_CMD} --outdir {dest} "
        f"--url https://github.com/openSUSE/obs-scm-bridge?subdir=test{fragment}",
    )


def test_creates_packagelist(auto_container_per_test: ContainerData):
    """Smoke test for the generation of the package list files `$pkg_name.xml`
    and `$pkg_name.info`:

    - verify that the destination folder contains all expected `.info` and
      `.xml` files
    - check the `scmsync` elements in the `.xml` files
    - check the HEAD hashes in the `.info` files
    """
    dest = "/tmp/ring0"
    auto_container_per_test.connection.run_expect(
        [0],
        f"{_OBS_SCM_BRIDGE_CMD} --outdir {dest} --url file://{_RPMS_DIR}ring0 --projectmode 1",
    )
    libeconf_hash, aaa_base_hash = (
        auto_container_per_test.connection.file(
            f"/src/{pkg_name}"
        ).content_string.strip()
        for pkg_name in ("libeconf", "aaa_base")
    )

    files = auto_container_per_test.connection.file(dest).listdir()
    assert len(files) == 4
    for file_name in (
        f"{pkg}.{ext}"
        for pkg, ext in product(("aaa_base", "libeconf"), ("xml", "info"))
    ):
        assert file_name in files

    def _test_pkg_xml(pkg_name: str, expected_url: str, expected_head_hash: str):
        conf = ET.fromstring(
            auto_container_per_test.connection.file(
                f"{dest}/{pkg_name}.xml"
            ).content_string
        )
        assert conf.attrib["name"] == pkg_name
        scm_sync_elements = conf.findall("scmsync")
        assert len(scm_sync_elements) == 1 and scm_sync_elements[0].text
        assert f"{expected_url}#{expected_head_hash}" in scm_sync_elements[0].text

    _test_pkg_xml("aaa_base", _AAA_BASE_URL, aaa_base_hash)
    _test_pkg_xml("libeconf", f"{_RPMS_DIR}libeconf", libeconf_hash)

    for pkg_name, pkg_head_hash in (
        ("aaa_base", aaa_base_hash),
        ("libeconf", libeconf_hash),
    ):
        assert (
            pkg_head_hash
            == auto_container_per_test.connection.file(
                f"{dest}/{pkg_name}.info"
            ).content_string.strip()
        )


def test_checks_out_project_without_submodules(
    auto_container_per_test: ContainerData,
) -> None:
    """Smoke test that we can clone a git repository that contains packages as
    sub-directories but not as git submodules.

    Additionally, we verify that the $pkg.xml and $pkg.info files are present
    and their contents are sane.

    """
    dest = "/tmp/proj"
    repo = f"file://{_RPMS_DIR}proj"
    auto_container_per_test.connection.check_output(
        f"{_OBS_SCM_BRIDGE_CMD} --outdir {dest} --url {repo} --projectmode 1",
    )

    for pkg_name in ("libeconf", "aaa_base"):
        info_file = auto_container_per_test.connection.file(f"{dest}/{pkg_name}.info")
        assert info_file.exists

        xml_file = auto_container_per_test.connection.file(f"{dest}/{pkg_name}.xml")
        assert xml_file.exists

        # the xml file should have essentially only the following contents:
        # <package name="$pkg_name">
        # <scmsync>$clone_url?subdir=$pkg_name</scmsync>
        # </package>
        pkg_meta = ET.fromstring(xml_file.content_string)
        assert pkg_meta.attrib["name"] == pkg_name
        scm_sync = pkg_meta.findall("scmsync")
        assert (
            len(scm_sync) == 1
            and scm_sync[0].text
            and scm_sync[0].text == f"{repo}?subdir={pkg_name}"
        )


LFS_REPO = "https://src.opensuse.org/pool/trivy.git"


@pytest.mark.parametrize("fragment", ["#eab0f16835309c7e772f81d523bb47356f3e14f05de74bfa88eaf59d73712215"])
@pytest.mark.parametrize("query", ["?lfs=1"])
@pytest.mark.parametrize("container_per_test", [TUMBLEWEED, LEAP_LATEST], indirect=True)
def test_downloads_lfs(container_per_test: ContainerData, fragment: str, query: str):
    """Test that the lfs file is automatically downloaded from the lfs server on
    clone.

    """
    _DEST = "/tmp/lfs-example"
    container_per_test.connection.run_expect(
        [0], f"{_OBS_SCM_BRIDGE_CMD} --outdir {_DEST} --url {LFS_REPO}{query}{fragment}"
    )

    tar_archive = container_per_test.connection.file(f"{_DEST}/trivy-0.54.1.tar.zst")
    assert tar_archive.exists and tar_archive.is_file
    assert tar_archive.size > 10 * 1024


@pytest.mark.parametrize("fragment", ["#eab0f16835309c7e772f81d523bb47356f3e14f05de74bfa88eaf59d73712215"])
def test_lfs_opt_out(auto_container_per_test: ContainerData, fragment: str):
    _DEST = "/tmp/lfs-example"
    auto_container_per_test.connection.run_expect(
        [0], f"{_OBS_SCM_BRIDGE_CMD} --outdir {_DEST} --url {LFS_REPO}?lfs=0{fragment}"
    )

    tar_archive = auto_container_per_test.connection.file(
        f"{_DEST}/trivy-0.54.1.tar.zst"
    )
    assert tar_archive.exists and tar_archive.is_file
    assert tar_archive.size < 1024
    assert "version https://git-lfs.github.com/spec" in tar_archive.content_string


@pytest.mark.parametrize(
    "git_repo_url,expected_head",
    [
        (f"{_LIBECONF_URL}{fragment}", commit)
        for fragment, commit in (
            ("", None),
            ("#master", None),
            (f"{pref}892dc9b83009c859ecfde218566a242241b95ad7" for pref in ("#", "")),
            ("#v0.4.5", "c9658f240b5c6d8d85f52f5019e47bc29c88b83f"),
        )
    ],
)
def test_clone_commit(
    auto_container_per_test: ContainerData,
    git_repo_url: str,
    expected_head: Optional[str],
):
    """Check that the service can clone libeconf at certain commits/branches or
    tags and verify that `HEAD` is at the correct commit.

    """
    _DEST = "/tmp/libeconf"
    auto_container_per_test.connection.run_expect(
        [0], f"{_OBS_SCM_BRIDGE_CMD} --outdir {_DEST} --url {git_repo_url}"
    )

    head = auto_container_per_test.connection.run_expect(
        [0], f"git -C {_DEST} rev-parse HEAD"
    ).stdout.strip()

    if expected_head:
        assert head == expected_head


@pytest.mark.parametrize(
    "env_var,shallow",
    [("", True), ("OSC_VERSION=1", False)],
)
def test_fetch_depth(
    auto_container_per_test: ContainerData, env_var: str, shallow: bool
):
    _DEST = "/tmp/libeconf"
    auto_container_per_test.connection.run_expect(
        [0], f"{env_var} {_OBS_SCM_BRIDGE_CMD} --outdir {_DEST} --url {_LIBECONF_URL}"
    )

    history_length = len(
        auto_container_per_test.connection.run_expect(
            [0], f"git -C {_DEST} log --oneline"
        )
        .stdout.strip()
        .splitlines()
    )
    if shallow:
        assert history_length == 1
    else:
        assert history_length > 1
