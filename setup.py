from setuptools import setup

setup(
    name="stm",
    version="0.2.5",
    description="A software transactional memory library",
    author="Alexander Boyd",
    author_email="alex@opengroove.org",
    packages=["stm"],
    install_requires=["ttftree", "six"]
)
