#!/bin/sh
# Mirrors the real business service's start-arthas.sh, so the demo
# container layout matches the real service layout exactly:
#   tools live at /usr/app/tools/arthas
#   Arthas version is 4.1.1
#   the .arthas copy step uses /usr/app/.arthas/lib/4.1.1
#
# This script is for a human operator in the Pod console, the same way
# the real service uses it. The MCP server does NOT call this script:
# it runs arthas-client.jar directly (and attaches with arthas-core.jar
# if the agent is not attached yet), so it works whether or not
# Arthas was started by this script first, and it does not need the
# system telnet binary that as.sh checks for.
JAVA_TOOL_OPTIONS=""
mkdir -p /usr/app/.arthas/lib/4.1.1

cp -a /usr/app/tools/arthas /usr/app/.arthas/lib/4.1.1/

java -Darthas.home=$HOME -jar /usr/app/tools/arthas/arthas-boot.jar
