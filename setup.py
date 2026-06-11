#!/usr/bin/env python3
import re
from pathlib import Path
from setuptools import setup

# Single source of truth: __init__.py
init = Path(__file__).parent / "__init__.py"
version = re.search(
    r'__version__\s*=\s*"([^"]+)"',
    init.read_text(encoding="utf-8"),
).group(1)

setup(
    name="agent_bus",
    version=version,
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
