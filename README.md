# Tangent - ML Researcher AI Agent for Tangle

## What is Tangle?

**Tangle** is an open source, platform-agnostic ML experimentation platform with a powerful drag-and-drop visual editor.
To learn more, visit <https://tangleml.com/> or the project repo: <https://github.com/TangleML/tangle>

## Tangent = Tangle + agents

**Tangent** is an autonomous ML engineering agent designed to accelerate your Tangle experimentation workflows.

There are several parts that make up Tangent:
* Tangent skills and agents. And Tangle CLI that the skills use. <https://github.com/TangleML/tangle-cli/tree/master/skills/tangent>
* Tangent Shell. A multi-agent orchestration platform and UI. <https://github.com/TangleML/tangent-shell>
* Tangent Agent Hosting. A solution for hosting remote agent instances that communicate with Tangle, cloud providers, and other external services. <https://github.com/TangleML/tangent>

## How to use Tangent?

* [Local](#use-tangent-locally):
* * [Local agent + Tangent Skills](#option-1-use-tangent-skills-with-your-favorite-agent-harness) + [Tangle CLI](#use-tangent-locally)
* * [Local Tangent Shell](#option-2-local-tangent-shell) + [Tangle CLI](#use-tangent-locally)
* Remote:
* * Single-instance remote Tangent Shell - WIP
* * Multi-instance remote Tangent Agent Hosting - WIP

### Use Tangent locally

Configure Tangle CLI to point it to Tangle API.

To verify that everything works, try uvx tangle-cli api secrets list

#### Local Tangle API:

Install and run Tangle locally if needed: https://github.com/TangleML/tangle#try-on-local-machine
Point the Tangle CLI to Tangle API:
```shell
export TANGLE_API_URL="http://localhost:8000"
```

#### Remote Tangle API:
Configure Tangle API URL and authentication. See <https://github.com/TangleML/tangle-cli#common-parameters-and-environment>

```shell
export TANGLE_API_URL="https://<your Tangle API URL>"
# Tangle auth (optional)
export TANGLE_API_TOKEN=<Bearer token>
export TANGLE_API_HEADERS=<custom auth headers>
```

### Option 1: Use Tangent skills with your favorite agent harness

Install Tangent skills:
```shell
npx skills add --global https://github.com/TangleML/tangle-cli/tree/master/skills/tangent
```

Choose the agents you're using and install the skills.


Then use the Tangent skills and agents with your favorite agent harness.

### Option 2: Local Tangent Shell

1. [Install Tangent Shell](https://github.com/TangleML/tangent-shell#getting-started)

1. [Configure Tangent Shell](
https://github.com/TangleML/tangent-shell#configuration)

## Remote Tangent installation


### Option 1 (WIP): Single-instance remote Tangent Shell on HuggingFace

WIP. Check back soon.

### Option 2: Single-instance remote Tangent Shell

Manually deploy a remote Tangle Shell instance based on the [Local Tangent Shell instructions](#option-2-local-tangent-shell)

### Option 3: Multi-instance remote Tangent Agent Hosting

Enterprise Kubernetes users can use the code in this Git repo (<https://github.com/TangleML/tangent>) as blueprint for Tangent Agent Hosting backend. See [Tangent Agent Hosting Platform](#tangent-agent-hosting-platform) and [Kubernetes README](/README_KUBERNETES.md).

## Tangent Agent Hosting Platform
Tangent Agent Hosting Platform helps users deploy persistent Tangent instances that communicate with Tangle, cloud providers, and other external services. Each Tangent instance is a multi-agent space: a Linux-based VM/container that can host multiple agentic apps (TUI, API, WebUI). Instance data (like agent sessions and memories) is persisted across restarts. There are also cross-instance shared memories.

#### Auth Proxy:
Agents need access to services, but there is always a risk of agents reading the credentials, thus leaking them to AI providers. Tangent solves this by adding a system-wide proxy which lives in a separate container. The proxy intercepts and modifies HTTP requests coming from the agentic tools. Auth proxy automatically adds auth headers and can modify request URLs (e.g. redirect api.aicompany.com to some AI proxy). To modify HTTPS requests, auth proxy creates new SSL certificates on the fly. The agent container’s OS and programs are configured to trust those certificates via a generated certificate authority.
