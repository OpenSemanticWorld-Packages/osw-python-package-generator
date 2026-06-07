import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import autoflake
import black
import isort
from git import InvalidGitRepositoryError
from osw.auth import CredentialManager
from osw.core import OSW
from osw.wtsite import WtPage, WtSite

_logger = logging.getLogger(__name__)

script_version = "0.2.3"

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

default_repo_org = "OpenSemanticWorld-Packages"

# packages in other GitHub orgs (e.g. upstream dependencies)
repo_org_overrides = {}

# prefixes to strip when deriving python package name from schema package name
# e.g. "world.opensemantic.core" -> "opensemantic.core-python"
# packages not matching any prefix keep their full name
python_package_prefix_strip = [
    "world.",
]


def _get_repo_org(package_name: str) -> str:
    """Resolve the GitHub org for a package name."""
    for prefix, org in repo_org_overrides.items():
        if package_name.startswith(prefix):
            return org
    return default_repo_org


def _get_python_package_name(package_name: str) -> str:
    """Derive python package name from schema package name.

    E.g. "world.opensemantic.core" -> "opensemantic.core-python"
    """
    for prefix in python_package_prefix_strip:
        if package_name.startswith(prefix):
            return package_name[len(prefix) :] + "-python"
    return package_name + "-python"


def _build_request(url: str, github_token: str | None = None) -> urllib.request.Request:
    """Build a urllib Request, adding Authorization header if token is provided."""
    req = urllib.request.Request(url)
    if github_token:
        # fine-grained tokens (github_pat_) use Bearer, classic PATs (ghp_) use token
        auth_scheme = "Bearer" if github_token.startswith("github_pat_") else "token"
        req.add_header("Authorization", f"{auth_scheme} {github_token}")
        _logger.debug(
            f"Request: {url} with {auth_scheme} auth "
            f"(token prefix: {github_token[:10]}...)"
        )
    req.add_header("Accept", "application/vnd.github+json")
    return req


def get_lastest_version(package_name, github_token: str | None = None):
    # determine latest tag
    git_url = "https://github.com/" + _get_repo_org(package_name) + "/" + package_name
    # e.g. https://api.github.com/repos/OpenSemanticWorld-Packages/world.opensemantic.core/tags
    package_versions_url = (
        git_url.replace("github.com", "api.github.com/repos") + "/tags"
    )
    # fetch JSON document with request lib
    req = _build_request(package_versions_url, github_token)
    try:
        with urllib.request.urlopen(req) as response:
            tags = json.load(response)
            if tags:
                package_version = tags[0]["name"]
                return package_version
    except urllib.error.HTTPError as e:
        _logger.error(
            f"HTTP {e.code} fetching {package_versions_url}: {e.read().decode()}"
        )
        raise
    return None


def download_schema_package(
    package_name, package_version=None, github_token: str | None = None
) -> WtSite.ReadPagePackageResult:
    # define repo url, e.g. https://github.com/OpenSemanticWorld-Packages/world.opensemantic.core
    git_url = "https://github.com/" + _get_repo_org(package_name) + "/" + package_name

    if package_version is None:
        package_version = get_lastest_version(package_name, github_token)

    if package_version is None:
        raise ValueError(
            f"No tags found for package {package_name} "
            f"in {_get_repo_org(package_name)} - cannot determine version"
        )

    _logger.info(f"Downloading schema package {package_name} version {package_version}")

    git_zip_url = git_url + "/archive/refs/tags/" + package_version + ".zip"
    # download package as zip
    # using a temp dir
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "downloaded.zip")

        # S310 Audit URL open for permitted schemes.
        if not git_zip_url.startswith(("http:", "https:")):
            raise ValueError("URL must start with 'http:' or 'https:'")

        # Download the ZIP file
        req = _build_request(git_zip_url, github_token)
        with urllib.request.urlopen(req) as response, open(zip_path, "wb") as f:  # nosec S310, url validated
            f.write(response.read())

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
    offline_pages: dict[str, WtPage],
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
    # create empty files if the do not exists
    python_code_path.touch(exist_ok=True)
    python_code_path_v1.touch(exist_ok=True)

    res = osw_obj.fetch_schema(
        fetchSchemaParam=OSW.FetchSchemaParam(
            schema_title=schema_titles,
            offline_pages=offline_pages,
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
            offline_pages=offline_pages,
            result_model_path=python_code_path_v1,
            mode="replace",
            generator_options={
                "output_model_type": "pydantic.BaseModel",
                "disable_timestamp": True,
            },
        )
    )

    # check if all fetched schemas are in the offline pages
    if set(res.fetched_schema_titles) - set(offline_pages.keys()):
        _logger.warning(
            f"Not all fetched schemas are in the offline pages: "
            f"{set(res.fetched_schema_titles) - set(offline_pages.keys())}"
        )
    if res.warning_messages:
        for warning_message in res.warning_messages:
            _logger.warning(f"Schema fetch warning: {warning_message}")
    if res.error_messages:
        for error_message in res.error_messages:
            _logger.error(f"Schema fetch error: {error_message}")

    return [python_code_path, python_code_path_v1]


def _find_dep_python_root(
    dep_python_package_name: str,
    python_code_working_dir_root: Path,
    dependency_python_roots: list[Path],
) -> Path:
    """Find the root directory containing a dependency python package."""
    for root in [python_code_working_dir_root, *dependency_python_roots]:
        candidate = root / dep_python_package_name / "src"
        if candidate.exists():
            return root
    return python_code_working_dir_root


def replace_duplicated_classes_with_imports(  # noqa: C901
    package_index,
    package_name,
    python_code_working_dir_root,
    python_code_working_dir,
    python_code_filename,
    dependency_python_roots: list[Path] | None = None,
):
    if dependency_python_roots is None:
        dependency_python_roots = []
    for subpath in ["", "v1"]:
        imports: dict[str, str] = {}
        for dep in package_index:
            dep_python_package_name = _get_python_package_name(dep)
            if dep != package_name:
                # open the result _model.py file
                dep_root = _find_dep_python_root(
                    dep_python_package_name,
                    python_code_working_dir_root,
                    dependency_python_roots,
                )
                dep_python_code_working_dir = dep_root / dep_python_package_name / "src"
                for component in dep_python_package_name.replace("-python", "").split(
                    "."
                ):
                    dep_python_code_working_dir /= component
                if subpath != "":
                    dep_python_code_working_dir /= subpath
                with open(
                    dep_python_code_working_dir / python_code_filename,
                    encoding="utf-8",
                ) as dep_file:
                    dep_code = dep_file.read()
                    pattern = re.compile(
                        r"^class\s*([\S]*)\s*\(\s*[\S\s]*?\s*\)\s*:.*\n", re.MULTILINE
                    )  # match class definition [\s\S]*(?:[^\S\n]*\n){2,}
                    classes = re.findall(pattern, dep_code)
                _logger.info(f"Imported classes from {dep}: {classes}")

                imports[dep_python_package_name.replace("-python", "")] = classes

        _python_code_working_dir = python_code_working_dir
        if subpath != "":
            _python_code_working_dir /= subpath

        with open(
            _python_code_working_dir / python_code_filename, encoding="utf-8"
        ) as file:
            content = file.read()
        for dep_python_package_name, classes in imports.items():
            import_stms = []
            import_path = dep_python_package_name
            if subpath != "":
                import_path += "." + subpath
            for c in classes:
                content_size = len(content)
                content = re.sub(
                    r"^(class\s*"
                    + c
                    + r"\s*\(\s*[\S\s]*?\s*\)\s*:.*\n[\s\S]*?(?:[^\S\n]*\n){3,})",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )  # replace duplicated classes
                if len(content) < content_size:
                    # add import if class was removed
                    import_stms.append(f"from {import_path} import {c}")

                # remove also any
                # <class>.update_forward_refs()
                # or
                # <class>.model_rebuild()
                content = re.sub(
                    "^" + c + re.escape(".update_forward_refs()") + "\n",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )
                content = re.sub(
                    "^" + c + re.escape(".model_rebuild()") + "\n",
                    "",
                    content,
                    count=1,
                    flags=re.MULTILINE,
                )

            content = "\n".join(import_stms) + "\n" + content

        # hot replacements
        hotfix_replacements = {
            # remain error in datamodel-codegen - unresolved titles
            # in Association and other OUs
            r"([l|L]ist)\[OSW44deaa5b806d41a2a88594f562b110e9\]": r"\1[Person]",
            # in Sampling
            r"([l|L]ist)\[OSW3d238d05316e45a4ac95a11d7b24e36b\]": r"\1[Location]",
            r": OSW3d238d05316e45a4ac95a11d7b24e36b": r": Location",
            r"([l|L]ist)\[OSWe427aafafbac4262955b9f690a83405d\]": r"\1[Tool]",
        }
        for key, value in hotfix_replacements.items():
            content = re.sub(key, value, content)

        # deduplicate identical classes within the same file
        # e.g. Tool and Tool1 generated from the same schema via different $ref paths
        class_pattern = re.compile(
            r"^(class\s+(\w+)\s*\([^)]*\)\s*:.*\n(?:(?!^class\s).*\n)*)",
            re.MULTILINE,
        )
        class_bodies = {}  # name -> (full_match, body_without_classname)
        for m in class_pattern.finditer(content):
            full_match = m.group(1)
            class_name = m.group(2)
            # Normalize: replace the class name in the definition to compare bodies
            body = re.sub(
                r"^class\s+" + re.escape(class_name) + r"(\s*\()",
                r"class __PLACEHOLDER__\1",
                full_match,
                count=1,
            )
            class_bodies[class_name] = (full_match, body)

        for class_name, (full_match, body) in list(class_bodies.items()):
            # Check if this is a numbered variant (e.g. Tool1, IDAndCountry1)
            m = re.match(r"^(.+?)(\d+)$", class_name)
            if m:
                base_name = m.group(1)
                if base_name in class_bodies:
                    _, base_body = class_bodies[base_name]
                    if body == base_body:
                        _logger.info(
                            f"Removing duplicate class {class_name} "
                            f"(identical to {base_name})"
                        )
                        content = content.replace(full_match, "")
                        # Replace any references to the numbered class
                        content = re.sub(
                            r"\b" + re.escape(class_name) + r"\b",
                            base_name,
                            content,
                        )

        # UUID-based deduplication: detect classes that share a UUID with a
        # dependency class but have a different name (e.g., SamplingInterval
        # has the same UUID as Time from characteristics.quantitative).
        # Replace with a shallow subclass that imports the dependency class.
        uuid_pattern = re.compile(r'"uuid":\s*"([^"]+)"')
        dep_uuids = {}  # uuid -> (dep_package, class_name)
        for dep_pkg, _classes in imports.items():
            dep_root = _find_dep_python_root(
                dep_pkg + "-python",
                python_code_working_dir_root,
                dependency_python_roots,
            )
            dep_dir = dep_root / (dep_pkg + "-python") / "src"
            for component in dep_pkg.split("."):
                dep_dir /= component
            dep_file = (
                dep_dir / subpath / python_code_filename
                if subpath
                else dep_dir / python_code_filename
            )
            if dep_file.exists():
                dep_code = dep_file.read_text(encoding="utf-8")
                for cm in class_pattern.finditer(dep_code):
                    dep_class_body = cm.group(1)
                    dep_class_name = cm.group(2)
                    uuid_m = uuid_pattern.search(dep_class_body)
                    if uuid_m:
                        dep_uuids[uuid_m.group(1)] = (dep_pkg, dep_class_name)

        if dep_uuids:
            for class_name, (full_match, _body) in list(class_bodies.items()):
                uuid_m = uuid_pattern.search(full_match)
                if uuid_m and uuid_m.group(1) in dep_uuids:
                    dep_pkg, dep_class_name = dep_uuids[uuid_m.group(1)]
                    if class_name != dep_class_name:
                        import_path = dep_pkg
                        if subpath:
                            import_path += "." + subpath
                        # Check if this class is actually used elsewhere
                        # (not just its own definition). Use negative
                        # lookbehind for _ to avoid matching _ClassName
                        usage_count = (
                            len(
                                re.findall(
                                    r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)",
                                    content,
                                )
                            )
                            - 1
                        )  # subtract the class definition itself
                        if usage_count <= 0:
                            # Unused - just remove entirely
                            _logger.info(
                                f"Removing unused class {class_name} "
                                f"(same UUID as {dep_class_name} "
                                f"from {dep_pkg})"
                            )
                            content = content.replace(full_match, "")
                        else:
                            _logger.info(
                                f"Replacing {class_name} with shallow "
                                f"subclass of {dep_class_name} from "
                                f"{dep_pkg} (same UUID: {uuid_m.group(1)})"
                            )
                            shallow = (
                                f"class {class_name}({dep_class_name}):\n    pass\n\n\n"
                            )
                            content = content.replace(full_match, shallow)
                            # Ensure the dep class is imported
                            import_line = f"from {import_path} import {dep_class_name}"
                            if import_line not in content:
                                content = import_line + "\n" + content

        # Remove pass-only subclasses that merely re-wrap an imported class
        # e.g. class Tool1(Tool):\n    pass\n when Tool is imported
        pass_class_pattern = re.compile(
            r"^class\s+(\w+)\s*\((\w+)\)\s*:\s*\n\s+pass\s*\n+",
            re.MULTILINE,
        )
        for m in pass_class_pattern.finditer(content):
            class_name = m.group(1)
            base_name = m.group(2)
            # Check if base is imported (not defined locally)
            if re.search(
                r"^class\s+" + re.escape(base_name) + r"\s*\(", content, re.MULTILINE
            ):
                continue  # base is locally defined, keep the subclass
            # Skip SamplingInterval/RefreshInterval-style classes that
            # are intentional shallow subclasses created by UUID dedup
            # (they will later get field overrides from allOf schemas)
            # Remove model_rebuild/update_forward_refs before counting
            content = re.sub(
                r"^" + re.escape(class_name) + r"\.model_rebuild\(\)\n",
                "",
                content,
                flags=re.MULTILINE,
            )
            content = re.sub(
                r"^" + re.escape(class_name) + r"\.update_forward_refs\(\)\n",
                "",
                content,
                flags=re.MULTILINE,
            )
            # Check if this class is used anywhere besides its definition
            usage_count = (
                len(
                    re.findall(
                        r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)", content
                    )
                )
                - 1
            )  # subtract class def
            if usage_count <= 0:
                _logger.info(
                    f"Removing orphaned pass-only class {class_name}({base_name})"
                )
                content = content.replace(m.group(0), "")
            else:
                # Class is used but is just a pass-only wrapper of an
                # imported base. Replace all references with the base.
                _logger.info(
                    f"Collapsing pass-only class {class_name} into "
                    f"{base_name} (replacing all references)"
                )
                content = content.replace(m.group(0), "")
                content = re.sub(
                    r"(?<![_\w])" + re.escape(class_name) + r"(?!\w)",
                    base_name,
                    content,
                )

        # run formatting tool black on the combined content
        # consolidate imports as well
        try:
            content = black.format_str(content, mode=black.Mode())
            # run isort to sort imports using Vertical Hanging Indent style
            content = isort.code(content, profile="black")

            content = autoflake.fix_code(content, remove_all_unused_imports=True)
        except Exception as e:
            _logger.error(f"Formatting failed: {e}")

        with open(
            _python_code_working_dir / python_code_filename, "w", encoding="utf-8"
        ) as file:
            file.write(content)


def build_packages(
    packages: list[str],
    python_code_working_dir_root: Path,
    commit: bool = False,
    github_token: str | None = None,
    dependency_python_roots: list[Path] | None = None,
    repo_org: str | None = None,
    repo_org_map: dict[str, str] | None = None,
):
    global default_repo_org, repo_org_overrides
    if repo_org is not None:
        default_repo_org = repo_org
    if repo_org_map is not None:
        repo_org_overrides = repo_org_map
    for package in packages:
        package_name = package.split("@")[0]
        package_version = package.split("@")[1] if "@" in package else None
        if package_version is None:
            package_version = get_lastest_version(package_name, github_token)

        # define python repo url,
        # e.g. https://github.com/OpenSemanticWorld-Packages/opensemantic.core-python
        python_package_name = _get_python_package_name(package_name)
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
        # note: pypi truncates the post number by removing leading zeros
        run_number = 0
        python_version_number = (
            package_version
            + ".post1"
            + script_version.split(".")[0].zfill(3)
            + script_version.split(".")[1].zfill(3)
            + script_version.split(".")[2].zfill(3)
            + str(run_number).zfill(3)
        )

        _logger.info(
            f"Building package {python_package_name} version {python_version_number}"
            f"at {python_code_working_dir}"
        )

        package_index: dict[str, dict[str, WtPage]] = {}
        result = download_schema_package(package_name, package_version, github_token)
        p_key = result.package_bundle.packages.keys().__iter__().__next__()
        dependencies = result.package_bundle.packages[p_key].requiredPackages
        while len(dependencies) > 0:
            dep = dependencies.pop()
            if dep not in package_index:
                dep_result = download_schema_package(dep, github_token=github_token)
                package_index[dep] = {page.title: page for page in dep_result.pages}
                dep_p_key = (
                    dep_result.package_bundle.packages.keys().__iter__().__next__()
                )
                dependencies.extend(
                    dep_result.package_bundle.packages[dep_p_key].requiredPackages
                )

        # collect all pages from all packages in the index
        pages = {page.title: page for page in result.pages}
        offline_pages = {
            page.title: page
            for pages in package_index.values()
            for page in pages.values()
        }
        # add pages to offline pages
        offline_pages.update(pages)

        python_code_paths = generate_python_dataclasses(
            pages, offline_pages, python_code_working_dir, python_code_filename
        )

        replace_duplicated_classes_with_imports(
            package_index,
            package_name,
            python_code_working_dir_root,
            python_code_working_dir,
            python_code_filename,
            dependency_python_roots=dependency_python_roots,
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


if __name__ == "__main__":
    # set log level info
    logging.basicConfig(level=logging.INFO)

    # prompt for GitHub token (optional, needed for private repos)
    _token = input("GitHub token (leave empty for public repos): ").strip() or None

    build_packages(
        # packages=["world.opensemantic.core@v0.53.2"],
        # packages=["world.opensemantic.base"],
        # packages=["world.opensemantic.lab"],
        packages=[
            # "world.opensemantic.core",
            "world.opensemantic.base",
            # "world.opensemantic.lab",
        ],
        python_code_working_dir_root=Path(__file__).parents[4] / "python_packages",
        commit=False,
        github_token=_token,
    )
