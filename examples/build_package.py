import logging
from pathlib import Path

from osw_python_package_generator import build_packages

if __name__ == "__main__":
    # set log level info
    logging.basicConfig(level=logging.INFO)

    build_packages(
        packages=["world.opensemantic.core@v0.53.2"],
        python_code_working_dir_root=Path(__file__).parents[3] / "python_packages",
        commit=False,
    )
