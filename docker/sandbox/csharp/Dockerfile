# Sandbox image for the CI Fixer v2 C# row.
#
# Stack detection maps any repo with *.csproj / *.sln / global.json to
# this image. .NET SDK 8 (LTS) — tests run via `dotnet test` and `dotnet
# format` handles lint. Coverlet attaches at test time through the MSBuild
# integration, so the repo only needs the `coverlet.msbuild` package
# reference (no extra runner).
#
# The GPT env planner is responsible for the repo's actual restore step
# (`dotnet restore`, `dotnet build --no-restore`, etc.); we only pre-bake
# the toolchain + a warm NuGet cache for the standard test/coverage deps.
#
# MUST be built with `--platform linux/amd64` on a Mac — prod Lightsail is
# amd64 and ARM images die under QEMU emulation (see memory note).

FROM mcr.microsoft.com/dotnet/sdk:8.0

# ── OS deps the env planner + agent tools need ───────────────────────────────
# git: for git_blame + commit_and_push sync
# curl, ca-certificates: for any curl-based NuGet mirror fetches
# jq: small diagnostic helper; useful inside run_in_sandbox
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq \
 && rm -rf /var/lib/apt/lists/*

# ── Warm the NuGet cache with xUnit + coverlet ───────────────────────────────
# Spins up a throwaway xUnit project and adds coverlet.msbuild so its
# transitive deps land in /root/.nuget/packages. Skips the first-run
# download storm inside each sandbox container. `|| true` because NuGet
# flakes are common at image-build time and a cold cache still works.
RUN mkdir -p /tmp/nuget-warm \
 && cd /tmp/nuget-warm \
 && dotnet new xunit -n Warm --force \
 && cd Warm \
 && dotnet add package coverlet.msbuild --version 6.0.0 \
 && dotnet restore \
 && cd / \
 && rm -rf /tmp/nuget-warm \
 || true

# Copy the shared sandbox reset script (same as python/node/go/rust/java).
COPY reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh \
 && mkdir -p /workspace \
 && chown -R 1000:1000 /workspace 2>/dev/null || true

WORKDIR /workspace
