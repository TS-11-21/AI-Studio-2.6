# AI Studio 2.6 — Source-Available Edition (Noncommercial)

AI Studio 2.6 organizes tasks in a list of pipelines. Each pipeline can have its own team and budget, and multiple AI members can be assigned within fixed roles. This source is prepared from the commercial-candidate client labeled 2.6. Actual purchase and cross-device authorization are not connected.

This repository is intended to make project-owned source code available for study and noncommercial distribution. Commercial use or sale of the software or source code, including providing them as part of a paid product or software service, requires separate authorization. As an express exception, users may use the software to create and sell their own works to which they hold lawful rights. Third-party dependencies remain subject to their own licenses. The LICENSE published with the repository will govern the detailed terms.

Compared with 1.0, version 2.6 adds a list of pipelines with a separate team and budget for each. The local concurrent-running limit of this candidate client is different from the number of configurations that can be saved.

Compared with 2.6, AI Studio 4.0 adds a free-form workstation graph, MCP plugin settings for each AI, production workstations using compatible general-purpose APIs, media uploads and access assignments, and main-AI-assisted planning. Actual capabilities depend on the selected models, tools, and configuration.

The planned AI Studio 4.0 commercial full version is intended to be provided as a program without source code. Purchase and download channels are not yet open. Company website: https://tsstudiohub.com/ — this is the company homepage, not a purchase or download link.

The software has not been formally released, and the 4.0 full version is not yet available for purchase.

## Run and configure

On Windows, install Python 3.12 or a compatible version, then run `python client/store_bootstrap.py` from the repository root. The application initializes a separate local data directory. Configure the assistants, models, and tools you need before starting; third-party services require their own setup, accounts, and billing.

[中文](README.md) · [English license](LICENSE) · [中文许可](LICENSE.zh-CN.txt)
