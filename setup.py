from setuptools import setup

setup(
    name="stm",
    version="0.2.2",
    description="A software transactional memory library",
    author="Alexander Boyd",
    author_email="alex@opengroove.org",
    packages=["stm"],
    install_requires=["ttftree"]
)
