# Demo image. Two deliberate choices, both matter for the Arthas demo:
# 1. JDK, not JRE: Arthas attach needs the JDK tooling.
# 2. Same user runs the app and later runs Arthas (root here, demo only).
#    In production, run as the same non-root user as the Java process.
FROM eclipse-temurin:17-jdk-jammy

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl unzip procps \
 && rm -rf /var/lib/apt/lists/*

# Arthas 4.0.5 full package (as.sh inside). Version is pinned so the demo
# does not break when "latest" moves. First attach may still download the
# Arthas core for the exact JDK, which needs outbound network once.
RUN mkdir -p /opt/arthas \
 && curl -L -o /tmp/arthas-bin.zip \
      https://github.com/alibaba/arthas/releases/download/arthas-all-4.0.5/arthas-bin.zip \
 && unzip -q /tmp/arthas-bin.zip -d /opt/arthas \
 && rm /tmp/arthas-bin.zip \
 && ls /opt/arthas

WORKDIR /app
COPY target/demo-service-*.jar /app/app.jar

EXPOSE 8080
# Xmx 512m vs container limit 768Mi is deliberate: it leaves room to talk
# about "limit vs Xmx + non-heap" without OOM-killing the demo immediately.
# G1 is the default here; keep it explicit so the memory story is checkable.
ENTRYPOINT ["java", "-Xms256m", "-Xmx512m", "-XX:+UseG1GC", "-jar", "/app/app.jar"]
