import logging
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from git import InvalidGitRepositoryError
from osw.auth import CredentialManager
from osw.core import OSW
from osw.wtsite import WtPage, WtSite

_logger = logging.getLogger(__name__)

script_version = "0.1.1"

python_code_filename = "_model.py"

# Create/update the password file under examples/accounts.pwd.yaml
pwd_file_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "accounts.pwd.yaml"
)

# login to demo.open-semantic-lab.org
osw_obj = OSW(
    site=WtSite(
        WtSite.WtSiteConfig(
            iri="wiki-dev.open-semantic-lab.org",
            cred_mngr=CredentialManager(cred_filepath=pwd_file_path),
        )
    )
)


def download_schema_package(
    package_name, package_version
) -> WtSite.ReadPagePackageResult:
    # define repo url, e.g. https://github.com/OpenSemanticWorld-Packages/world.opensemantic.core
    git_url = "https://github.com/OpenSemanticWorld-Packages/" + package_name
    git_zip_url = git_url + "/archive/refs/tags/" + package_version + ".zip"
    # download package as zip
    # using a temp dir
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "downloaded.zip")

        # S310 Audit URL open for permitted schemes.
        if not git_zip_url.startswith(("http:", "https:")):
            raise ValueError("URL must start with 'http:' or 'https:'")

        # Download the ZIP file
        urllib.request.urlretrieve(git_zip_url, zip_path)  # nosec S310, url validated

        # Extract the ZIP file
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # List all extracted files
        extracted_files = os.listdir(temp_dir)
        print(f"Extracted files: {extracted_files}")

        result = osw_obj.site.read_page_package(
            WtSite.ReadPagePackageParam(
                storage_path=os.path.join(
                    temp_dir,
                    package_name + "-" + package_version[1:]
                    if package_version.startswith("v")
                    else package_version,
                )
            )
        )
    return result


def is_git_repo(directory):
    """Check if directory is a git repository"""
    from git import Repo

    try:
        Repo(directory)
        return True
    except InvalidGitRepositoryError:
        return False


def commit_and_tag(directory, file_paths, commit_message, tag_name):
    """Commit a file and create a version tag in git repository using GitPython"""
    from git import Repo

    if not is_git_repo(directory):
        print(f"{directory} is not a git repository")
        return False

    try:
        # Open the repository
        repo = Repo(directory)

        # Add file to staging
        repo.index.add(file_paths)

        # Commit the file, precommit-hook disabled
        repo.index.commit(commit_message, skip_hooks=True)

        # Create version tag
        repo.create_tag(tag_name, message=f"Release {tag_name}")

        print(f"Successfully committed {file_paths} and created tag {tag_name}")
        return True

    except Exception as e:
        print(f"Error during git operation: {e}")
        return False


def generate_python_dataclasses(
    schema_pages: dict[str, WtPage],
    python_code_working_dir: Path,
    python_code_filename: Path,
) -> list[Path]:
    # filter schema pages
    page_list = list(schema_pages.keys())
    schema_titles = [title for title in page_list if title.startswith("Category:")]

    python_code_path = Path(python_code_working_dir)
    python_code_path /= python_code_filename
    python_code_path_v1 = Path(python_code_working_dir) / "v1"
    python_code_path_v1 /= python_code_filename
    # make sure code paths exist
    python_code_path.parent.mkdir(parents=True, exist_ok=True)
    python_code_path_v1.parent.mkdir(parents=True, exist_ok=True)

    res = osw_obj.fetch_schema(
        fetchSchemaParam=OSW.FetchSchemaParam(
            schema_title=schema_titles,
            offline_pages=schema_pages,
            result_model_path=python_code_path,
            mode="replace",
            generator_options={
                "output_model_type": "pydantic_v2.BaseModel",
                "disable_timestamp": True,
            },
        )
    )

    if res.warning_messages:
        for warning_message in res.warning_messages:
            _logger.warning(f"Schema fetch warning: {warning_message}")
    if res.error_messages:
        for error_message in res.error_messages:
            _logger.error(f"Schema fetch error: {error_message}")

    res = osw_obj.fetch_schema(
        fetchSchemaParam=OSW.FetchSchemaParam(
            schema_title=schema_titles,
            offline_pages=schema_pages,
            result_model_path=python_code_path_v1,
            mode="replace",
            generator_options={
                "output_model_type": "pydantic.BaseModel",
                "disable_timestamp": True,
            },
        )
    )

    # check if all fetched schemas are in the offline pages
    if set(res.fetched_schema_titles) - set(schema_pages.keys()):
        _logger.warning(
            f"Not all fetched schemas are in the offline pages: "
            f"{set(res.fetched_schema_titles) - set(schema_pages.keys())}"
        )
    if res.warning_messages:
        for warning_message in res.warning_messages:
            _logger.warning(f"Schema fetch warning: {warning_message}")
    if res.error_messages:
        for error_message in res.error_messages:
            _logger.error(f"Schema fetch error: {error_message}")

    return [python_code_path, python_code_path_v1]


def build_packages(
    packages: list[str], python_code_working_dir_root: Path, commit: bool = False
):
    for package in packages:
        package_name, package_version = package.split("@")

        # define python repo url,
        # e.g. https://github.com/OpenSemanticWorld-Packages/opensemantic.core-python
        python_package_name = package_name.replace("world.", "") + "-python"
        # python_git_url = (
        #     "https://github.com/OpenSemanticWorld-Packages/" + python_package_name
        # )
        python_code_working_dir = (
            python_code_working_dir_root / python_package_name / "src"
        )
        for component in python_package_name.replace("-python", "").split("."):
            python_code_working_dir /= component

        # define version:
        # package version + post + builder script version
        # as int (the digits per version component)
        # e.g. package_version = 0.53.0, script_version = 0.1.0
        # => 0.53.0.post000001000
        # additional 3 digits for the build number
        # in case different runs could lead to different results
        run_number = 0
        python_version_number = (
            package_version
            + ".post"
            + script_version.split(".")[0].zfill(3)
            + script_version.split(".")[1].zfill(3)
            + script_version.split(".")[2].zfill(3)
            + str(run_number).zfill(3)
        )

        _logger.info(
            f"Building package {python_package_name} version {python_version_number}",
            f"at {python_code_working_dir}",
        )

        result = download_schema_package(package_name, package_version)

        pages = {page.title: page for page in result.pages}

        python_code_paths = generate_python_dataclasses(
            pages, python_code_working_dir, python_code_filename
        )

        # if the target dir is a git repo, tag it with the python package version
        if commit:
            _logger.info(
                f"Done. Tag git repo at {python_code_working_dir}"
                f" with version {python_version_number}"
            )
            if is_git_repo(python_code_working_dir_root / python_package_name):
                commit_and_tag(
                    python_code_working_dir_root / python_package_name,
                    python_code_paths,
                    "generate code from " + package,
                    python_version_number,
                )
        else:
            _logger.info(f"Done. Skipping commit and tag for {python_package_name}")
