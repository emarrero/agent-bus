#!/usr/bin/env python3
from setuptools import setup

setup(
    name="agent_bus",
    version="0.1.0",
    description="Multi-agent communication network for AI agents",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="Efrain Marrero",
    url="https://github.com/emarrero/agent-bus",
    license="MIT",
    python_requires=">=3.10",
    packages=["agent_bus"],
    package_dir={"agent_bus": "."},
    package_data={"agent_bus": ["py.typed"]},
    include_package_data=True,
    extras_require={
        "ws": ["websockets"],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Communications",
        "Intended Audience :: Developers",
    ],
    entry_points={
        "console_scripts": [
            "agent-bus=agent_bus.cli:main",
        ],
    },
)
