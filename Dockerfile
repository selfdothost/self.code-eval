FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Base dependencies + Python + C++
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    curl wget gnupg2 ca-certificates \
    build-essential g++ \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS + TypeScript (multiple-js, multiple-ts)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g typescript \
    && rm -rf /var/lib/apt/lists/*

# Java + Scala (multiple-java, multiple-scala)
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk scala \
    && rm -rf /var/lib/apt/lists/*

# Go (multiple-go)
RUN apt-get update && apt-get install -y --no-install-recommends golang-go \
    && rm -rf /var/lib/apt/lists/*

# Ruby, PHP, Lua, R (multiple-rb, multiple-php, multiple-lua, multiple-r)
# Lua: MultiPL-E's eval_lua.py runs lua5.3, and the generated tests do
# `require('luaunit')`. Both the interpreter AND the luaunit library must be
# present or every lua test errors at load — that (not the 5.3/5.4 version) is
# why multiple-lua scored 0; with luaunit it jumps to ~0.8.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ruby php-cli lua5.3 r-base \
    && ln -sf /usr/bin/lua5.3 /usr/bin/lua \
    && rm -rf /var/lib/apt/lists/*
# luaunit: single-file pure-lua unit-test lib the MultiPL-E lua tests require().
# Drop it on lua5.3's package.path (/usr/local/share/lua/5.3/?.lua).
RUN mkdir -p /usr/local/share/lua/5.3 && \
    curl -fsSL --retry 5 -o /usr/local/share/lua/5.3/luaunit.lua \
      https://raw.githubusercontent.com/bluebird75/luaunit/LUAUNIT_V3_4/luaunit.lua

# Mono C# (multiple-cs) — uses csc + mono
RUN apt-get update && apt-get install -y --no-install-recommends \
    mono-mcs mono-runtime \
    && ln -sf /usr/bin/mcs /usr/bin/csc \
    && rm -rf /var/lib/apt/lists/*

# Racket (multiple-rkt)
RUN apt-get update && apt-get install -y --no-install-recommends racket \
    && rm -rf /var/lib/apt/lists/*

# Rust (multiple-rs)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

# Java test-harness dep (multiple-java): eval_java.py loads org.javatuples from
# this exact path. Without it, every javac of a program that returns a
# Pair/Triplet fails to compile — multiple-java scored 0.006. One small jar.
RUN mkdir -p /usr/multiple && \
    curl -fsSL --retry 5 -o /usr/multiple/javatuples-1.2.jar \
      https://repo1.maven.org/maven2/org/javatuples/javatuples/1.2/javatuples-1.2.jar

# D (multiple-d): eval_dlang.py runs `rdmd`, which ships with the reference dmd
# compiler + dtools (ldc does not provide rdmd). Install via the official
# dlang install script to a fixed prefix, then symlink the tools onto PATH.
RUN curl -fsSL --retry 5 https://dlang.org/install.sh -o /tmp/dinstall.sh && \
    bash /tmp/dinstall.sh install dmd -p /opt/dlang && \
    ln -sf /opt/dlang/dmd-*/linux/bin64/rdmd /usr/local/bin/rdmd && \
    ln -sf /opt/dlang/dmd-*/linux/bin64/dmd  /usr/local/bin/dmd && \
    rm -f /tmp/dinstall.sh

# Julia (multiple-jl): eval_julia.py runs `julia`. No reliable apt package;
# fetch the official pinned tarball.
ENV JULIA_VERSION=1.10.5
RUN curl -fsSL --retry 5 "https://julialang-s3.julialang.org/bin/linux/x64/1.10/julia-${JULIA_VERSION}-linux-x86_64.tar.gz" \
      | tar xz -C /opt && \
    ln -sf /opt/julia-${JULIA_VERSION}/bin/julia /usr/local/bin/julia
# Pre-warm the Test-stdlib precompile cache. eval_julia.py runs each program with
# timeout_seconds=5, but a COLD `using Test` precompile takes far longer, so the
# first julia problems time out (multiple-jl scored 0 on a cold pod, 1.0 warm).
# Baking the cache into /root/.julia makes every eval start warm.
RUN julia -e 'using Test; println("julia Test precompiled")'

# Swift (multiple-swift): eval_swift.py runs `swiftc`. Official Ubuntu 22.04
# toolchain + its runtime deps; add its bin dir to PATH.
ENV SWIFT_VERSION=5.10.1
RUN apt-get update && apt-get install -y --no-install-recommends \
      binutils libc6-dev libcurl4-openssl-dev libedit2 libgcc-11-dev \
      libpython3-dev libsqlite3-0 libstdc++-11-dev libxml2-dev libz3-dev \
      pkg-config tzdata unzip zlib1g-dev libncurses6 && \
    curl -fsSL --retry 5 "https://download.swift.org/swift-${SWIFT_VERSION}-release/ubuntu2204/swift-${SWIFT_VERSION}-RELEASE/swift-${SWIFT_VERSION}-RELEASE-ubuntu22.04.tar.gz" \
      | tar xz -C /opt && \
    rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/swift-${SWIFT_VERSION}-RELEASE-ubuntu22.04/usr/bin:${PATH}"

COPY . /app

WORKDIR /app

RUN test -f /app/generations.json && rm /app/generations.json || true

# CPU-only torch — harness calls remote API, doesn't need GPU/CUDA. Avoids
# pulling ~3-4GB of nvidia-* CUDA wheels that OOM-kill the kaniko snapshot.
RUN pip3 install torch --extra-index-url https://download.pytorch.org/whl/cpu && \
    pip3 install . && \
    rm -rf /root/.cache/pip

# DS-1000 test_code.py files use the deprecated `parser` module (removed in 3.12,
# also missing from some 3.10 rebuilds). Install a minimal compatibility shim.
RUN cp /app/parser_shim.py "$(python3 -c 'import site; print(site.getsitepackages()[0])')/parser.py"

RUN mkdir -p /workspace/results /workspace/logs

EXPOSE 8094

CMD ["python3", "/app/api/main.py"]
