import logging
from pathlib import Path

from osw_python_package_generator import build_packages

if __name__ == "__main__":
    # set log level info
    logging.basicConfig(level=logging.INFO)

    build_packages(
        # specific version
        # packages=["world.opensemantic.core@v0.53.2"],
        # latest version
        # packages=["world.opensemantic.core"],
        # multiple packages
        packages=[
            "world.opensemantic.core",
            "world.opensemantic.base",
            "world.opensemantic.lab",
        ],
        python_code_working_dir_root=Path(__file__).parents[3] / "python_packages",
        commit=False,
    )
