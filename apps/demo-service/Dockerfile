# Demo image, laid out like the real business service so the MCP
# server validated on this demo needs no re-adaptation on the real
# service:
#   app root: /usr/app (jar at /usr/app/service/app.jar, as in the
#     real service; its business port there is 8088, the demo keeps
#     8080 on purpose -- port is not an MCP input, MCP uses exec,
#     and changing the demo port would ripple through every k8s
#     manifest, smoke test and README NodePort example)
#   tools:    /usr/app/tools/arthas (Arthas 4.1.1, same as the real service;
#     as.sh sits at the top level there, alongside arthas-boot.jar)
#   operator script: /usr/app/tools/start-arthas.sh (same content/shape
#     as the real service's script; MCP does not call it, it uses
#     as.sh in batch mode directly)
#
# Two deliberate choices, both matter for the Arthas demo:
# 1. JDK, not JRE: Arthas attach needs the JDK tooling.
# 2. Same user runs the app and later runs Arthas (root here, demo only).
#    In production, run as the same non-root user as the Java process.
FROM eclipse-temurin:17-jdk-jammy

# telnet is REQUIRED even though MCP uses as.sh in batch mode (-c):
# as.sh checks for the telnet binary up front and exits 1 with
# "Error: telnet is not installed" if it is missing, before it ever
# attaches -- even though -c itself is then served by arthas-client.jar
# (Java), not by the telnet binary. This was verified on 4.0.5 and
# confirmed again on the real service's 4.1.1 as.sh (lines 189 and
# 202-203: "check curl/grep/awk/telnet/unzip command" and the
# telnet-missing error). Without it, every Arthas tool
# (dashboard/jvm/thread/monitor/trace/reset) fails the same way,
# while list_pods (kubectl only, no as.sh) still works. What is
# NOT yet confirmed is whether the real container actually has the
# telnet binary installed -- that is a separate check (command -v).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip procps telnet \
 && rm -rf /var/lib/apt/lists/*

# Arthas 4.1.1 full package (as.sh inside), at the real service's path.
# Version is pinned so the demo does not break when "latest" moves.
# First attach may still download the Arthas core for the exact JDK,
# which needs outbound network once. The CI Docker build is the check
# that this 4.1.1 package URL resolves; it was not verified by a
# separate web lookup in this change.
RUN mkdir -p /usr/app/tools/arthas \
 && curl -L -o /tmp/arthas-bin.zip \
      https://github.com/alibaba/arthas/releases/download/arthas-all-4.1.1/arthas-bin.zip \
 && unzip -q /tmp/arthas-bin.zip -d /usr/app/tools/arthas \
 && rm /tmp/arthas-bin.zip \
 && ls /usr/app/tools/arthas

WORKDIR /usr/app
RUN mkdir -p /usr/app/service
COPY target/demo-service-*.jar /usr/app/service/app.jar
COPY start-arthas.sh /usr/app/tools/start-arthas.sh
RUN chmod +x /usr/app/tools/start-arthas.sh

EXPOSE 8080
# Xmx 512m vs container limit 768Mi is deliberate: it leaves room to talk
# about "limit vs Xmx + non-heap" without OOM-killing the demo immediately.
# G1 is the default here; keep it explicit so the memory story is checkable.
ENTRYPOINT ["java", "-Xms256m", "-Xmx512m", "-XX:+UseG1GC", "-jar", "/usr/app/service/app.jar"]
