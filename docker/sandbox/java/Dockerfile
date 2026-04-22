# Sandbox image for the CI Fixer v2 Java row.
#
# Stack detection maps any repo with pom.xml / build.gradle / build.gradle.kts
# to this image. JDK 21 (current LTS) + Maven 3.9; Gradle handled via the
# repo's own wrapper (`./gradlew`).
#
# The GPT env planner is responsible for running the repo's actual install
# step (`mvn dependency:go-offline`, `./gradlew build -x test`, etc.) inside
# the checked-out sandbox — we only pre-bake the toolchain.

FROM maven:3.9-eclipse-temurin-21

# ── OS deps the env planner + agent tools need ───────────────────────────────
# git: for git_blame + commit_and_push sync
# curl, ca-certificates: for curl-based package fetches when pom/gradle
#   download plugins that shell out
# jq: small diagnostic helper; useful inside run_in_sandbox
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq \
 && rm -rf /var/lib/apt/lists/*

# ── Warm the Maven local cache with the two most common plugins ──────────────
# Skips a first-run download storm inside each sandbox container for the
# standard lint/test plugins. Compile/runtime deps stay repo-driven.
RUN mvn -B dependency:get \
      -Dartifact=com.puppycrawl.tools:checkstyle:10.14.2:jar \
      -Dtransitive=true \
 && mvn -B dependency:get \
      -Dartifact=org.jacoco:jacoco-maven-plugin:0.8.12:jar \
      -Dtransitive=true \
 || true
# `|| true` — network can be flaky at image-build time; individual runs
# still work because Maven falls back to the live repo.

# Copy the shared sandbox reset script (same as python/node/go/rust).
COPY reset.sh /phalanx/reset.sh
RUN chmod +x /phalanx/reset.sh \
 && mkdir -p /workspace \
 && chown -R 1000:1000 /workspace 2>/dev/null || true

WORKDIR /workspace
