# Arthas MCP 演示环境（Java + 本地 kind）

目标：给团队分享时，能现场演示“本地 Agent → 本地 MCP → Pod 内 Arthas →
JVM”这条链路，查两个场景：慢接口、Pod 内存上涨。演示用演示命名空间，
不碰生产。

## 两条部署路径，不要拿错文件（上次就是在这里拿错的）

| 路径 | 用途 | 命名空间 | 对象名 | 镜像 |
| --- | --- | --- | --- | --- |
| `k8s/namespace.yaml` + `k8s/deployment.yaml` + `k8s/service.yaml` | 本地 kind / CI 一次性集群 | `demo-arthas` | `demo-service` | 本地 `demo-service:0.1.0`（kind load） |
| `k8s/gzdt/deployment.yaml` + `k8s/gzdt/service.yaml` | 局域网 / 共享集群 | 已有 `gzdt`，不新建 | `arthas-demo-service` | GHCR `ghcr.io/xywhnh/arthas-mcp-demo:0.1.0`（公开可拉） |

`k8s/` 下的文件头都标了 `KIND / CI ONLY`，不要上传到局域网集群；
`k8s/gzdt/` 里只有 Deployment + Service，没有 namespace 文件。

## 目录结构（mono repo 样子）

```
arthas-mcp-demo/
  pom.xml                          # 聚合根
  apps/demo-service/               # Spring Boot 3.3 + Java 17 演示服务
    src/main/java/com/example/demo/
      order/OrderController.java   # GET /api/orders/{id}
      order/OrderService.java      # 四种模式：normal / self_slow /
                                   # downstream_slow / nplus1
      order/DownstreamClient.java  # 模拟下游：getItem / getItemSlow
      fault/FaultController.java   # 仅演示用开关：/demo/faults/*
      fault/FaultState.java        # 模式 + 无界缓存（内存泄漏）
    Dockerfile                     # JDK 17 + Arthas 4.0.5（非 JRE）
  k8s/                             # namespace / deployment(1 副本) / service
  scripts/                         # kind-up / build-and-deploy / smoke / arthas
  mcp/arthas_mcp_server.py         # 最小本地 MCP Server（Python，无外部依赖）
  mcp/mcp-config.example.json      # Agent 客户端接入示例（改绝对路径后用）
```

为什么是 1 副本：故障开关是 Pod 内内存状态。1 副本时“开故障前后”
是确定性的；走 Service 负载均衡到 2 副本时，开关只影响其中一个 Pod，
演示会忽快忽慢。想做健康 Pod vs 慢 Pod 对照时，扩到 2 副本并分别
port-forward 两个 Pod，不要走 Service。

## 前置条件（在你自己的机器上）

JDK 17、Maven、Docker、kind、kubectl。首次 attach Arthas 需要联网
（Arthas 会按 JDK 下载对应核心）。

Java 编译、镜像构建、kind 部署、GHCR 发布这四步已由 GitHub Actions
反复验证通过（CI 在 kind 上跑通健康检查、订单接口、故障开关；
Publish 把 amd64 + arm64 镜像推到公开 GHCR）。本地机器上按下面顺序
跑，任何一步失败先停在那一步，不要跳到 MCP。

## 快速开始（本地 kind，推荐）

```bash
scripts/kind-up.sh
scripts/build-and-deploy.sh
scripts/smoke.sh
```

冒烟通过后，先不经过 MCP，直接验证 Arthas 能 attach：

```bash
scripts/arthas.sh dashboard
scripts/arthas.sh jvm
```

## 局域网 gzdt 部署（Dashboard 上传 YAML）

只传这两个文件，不要传 `k8s/` 下的任何文件，也不要传 `namespace.yaml`：

- `k8s/gzdt/deployment.yaml`（`arthas-demo-service` / `gzdt` / GHCR 镜像，注意：镜像必须是 Docker v2 格式，局域网旧运行时拒绝 OCI 格式，Publish 流程里已强制 `oci-mediatypes=false` 并关掉 provenance/SBOM，且有发布后格式校验）
- `k8s/gzdt/service.yaml`（`arthas-demo-service` / `gzdt`）

步骤：Dashboard 左边命名空间先切到 `gzdt` → 确认部署和服务列表里
还没有 `arthas-demo-service` → 右上角 **+ 创建**，上传 YAML，先传
deployment 再传 service → 部署列表里看 Pod 是不是 1/1 Running。

验证（局域网机器终端，用你自己的 kubeconfig）：

```bash
kubectl -n gzdt rollout status deployment/arthas-demo-service --timeout=180s
kubectl -n gzdt port-forward svc/arthas-demo-service 8080:8080 &
curl -s http://localhost:8080/actuator/health; echo
curl -s http://localhost:8080/api/orders/1; echo
```

gzdt 下的 Arthas / MCP 也要跟着改环境变量，否则 MCP 按 kind 的
标签找不到 Pod：

```bash
NS=gzdt APP_LABEL=arthas-demo-service scripts/arthas.sh dashboard
NS=gzdt SVC=arthas-demo-service scripts/smoke.sh
```

MCP 配置用 `mcp/mcp-config.example.json` 里的 `arthas-demo-gzdt`
那一项（`ARTHAS_NAMESPACE=gzdt`、`ARTHAS_ALLOWED_NAMESPACES=gzdt`、
`ARTHAS_APP_LABEL=arthas-demo-service`），不要用 kind 那一项。

## 演示剧本 1：慢接口

先在两个终端准备好：一个持续打流量，一个跑 MCP / Arthas。

```bash
# 终端 A：持续流量（每 0.5 秒一次）
while true; do curl -s http://localhost:8080/api/orders/1 >/dev/null; sleep 0.5; done
```

演示顺序（每步都先让听众猜，再揭证据）：

1. 基线：`GET /api/orders/1`，mode=normal，约 20-50ms。
2. 开故障（只打到指定 Pod，不要走 Service）：
   ```bash
   kubectl -n demo-arthas port-forward pod/<pod> 8081:8080 &
   curl -X POST 'http://localhost:8081/demo/faults/mode?mode=nplus1'
   ```
3. Agent 提问示例：
   > 查 demo-service 这个接口在慢 Pod 上的调用画像，再 trace
   > `com.example.demo.order.OrderService getOrder`，拆自身耗时和下游耗时。
4. 证据对照：
   - `nplus1`：单次下游不慢，但 trace 里 `getItem` 出现约 50 次。
   - `downstream_slow`：`getItemSlow` 一次就占大头。
   - `self_slow`：`getOrder` 自身耗时占大头，下游只有一次且很快。
5. 结束必做：Agent 调 `arthas_reset`，再 `POST /demo/faults/reset`。

trace 需要流量配合：如果没有终端 A 的持续流量，trace 会一直等
到 count 满或超时，这是正常的，不要在现场干等。

## 演示剧本 2：Pod 内存上涨

```bash
# 终端 A：先给 Pod 打一点泄漏（50MB 一次，可重复打）
kubectl -n demo-arthas port-forward pod/<pod> 8081:8080 &
curl -X POST 'http://localhost:8081/demo/faults/leak?megabytes=50'
curl -s http://localhost:8081/demo/faults
```

Agent 提问示例：

> 连续看这个 Pod 的 dashboard / jvm，判断是堆内泄漏、Metaspace
> 泄漏、堆外上涨，还是容器配额问题，并给出最可疑的证据。

判断口诀（现场讲，不要只贴数字）：

- Old 区涨、Full GC 后不降 → 对象被长期持有（这里就是无界缓存）。
- 涨到高水位稳定、GC 频繁但不 OOM → 堆配小或分配速率高。
- Metaspace 涨 → 动态类 / 类加载器泄漏。
- 堆稳定但 Pod RSS 涨 → 先看堆外、线程。
- OOMKilled 但堆没满 → 先核对容器 limit（这里 768Mi）与 Xmx
  （这里 512m）+ 非堆的配比，别一上来就说是代码泄漏。

本演示故意不做 heapdump：heapdump 会触发 Full GC、文件大，
生产默认不开放；分享现场讲原理就够了。

## MCP 作为集群内独立服务（推荐：宿主机上不用跑任何东西）

上面说的“在局域网服务器上起一个进程”只是最快的验证办法，不是必须
的。在你的环境里，所有可用资源都是 k8s 上可部署的，物理机都组成
了集群，所以正确的形态就是：**MCP 本身也是一个独立的 Deployment +
Service，跟业务服务平级，不依赖任何一台宿主机上的进程。**

这个版本已经做好了，清单在 `k8s/gzdt/mcp-*.yaml`，全部是现有命名
空间 `gzdt`，不新建命名空间，不动已有服务：

- `mcp-serviceaccount.yaml`：ServiceAccount `arthas-mcp-server`
- `mcp-role.yaml`：Role，只给 `pods` 的 get/list 和 `pods/exec` 的
  create，不能删东西、不能读 Secret、不能改 Deployment/Service
- `mcp-rolebinding.yaml`：把 Role 绑给上面的 ServiceAccount
- `mcp-secret.yaml`：Bearer 口令，**上传前必须把里面的
  `REPLACE-ME-WITH-A-SECRET` 换成你自己的口令**，不要把真口令提交
  进仓库
- `mcp-deployment.yaml`：MCP 的 Deployment，镜像是
  `ghcr.io/xywhnh/arthas-mcp-demo-mcp:0.1.0`（Python + kubectl +
  MCP 代码的独立镜像，不是业务镜像，业务镜像里没有 Python 和
  kubectl，所以业务 Pod 里跑不起 MCP）
- `mcp-service.yaml`：NodePort Service，`nodePort` 不填，集群自动
  分配一个端口（跟业务服务拿到 32320 是一回事）

为什么 Pod 里的 MCP 不需要 kubeconfig、也不需要宿主机上的 kubectl：
Pod 启动时，集群会自动把这个 ServiceAccount 的 token 挂载进 Pod，
Pod 里的 kubectl 会用这个 token 和集群内地址
（`https://kubernetes.default.svc`）直接连 API Server，权限就是上面
那个 Role 给的、只限 gzdt 的范围。所以 MCP Pod 一起，诊断通道就
通了，宿主机上什么都不用跑。

Dashboard 上传顺序就是上面列的 1 到 6，Service 上传完后去服务列表
里看它被分配的 NodePort，然后在你本机上验：

```bash
curl -s -H 'Content-Type: application/json' \
     -H 'Authorization: Bearer <你在 Secret 里填的口令>' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://10.129.73.14:<上面那个 NodePort>/mcp
# 预期：返回 9 个工具；不带口令返回 401
```

注意：MCP 镜像是新包，第一次 Publish 之后默认是 private，跟之前业
务镜像一样，需要你去把它的可见性改成 public，否则 MCP Pod 会
ImagePullBackOff。MCP 镜像同样是 Docker v2 格式，Publish 里有校验。

## 通用接 MCP：客户端不需要 kubectl（推荐，演示和团队用这个）

stdio 版只适合 MCP 和 Agent 在同一台机器上、且那台机器上有 kubectl
的场景，本机没有 kubectl 的用户用不了，那不是通用 MCP。

通用版是 `mcp/mcp_http_server.py`（Python 标准库，无第三方依赖，
和 stdio 版共用同一套工具和安全规则）：

- **客户端要求只有一个：能用 HTTP 连到 MCP 服务器。** 不需要 kubectl、
  不需要 Python、不需要 kubeconfig、不需要本地文件。
- **服务器端跑在能连集群的机器上**（比如局域网服务器），它内部调
  kubectl exec 去 Pod 里跑 Arthas，kubectl 只是服务器端的实现细节，
  客户端完全看不到它。
- **一个服务器能查多个服务**：每个工具都接受可选的 `namespace`
 （必须在服务器的 `ARTHAS_ALLOWED_NAMESPACES` 白名单里）和
  `app_label`（Pod 标签值，键是 `app`），不填就用服务器默认值。
  所以同一个 MCP 服务器既能查 kind 的 `demo-arthas` / `demo-service`，
  也能查局域网的 `gzdt` / `arthas-demo-service`，不用为每个服务
  再起一个 MCP。

在能连集群的机器上（比如局域网服务器）启动：

```bash
ARTHAS_ALLOWED_NAMESPACES=gzdt ARTHAS_NAMESPACE=gzdt \
ARTHAS_APP_LABEL=arthas-demo-service MCP_AUTH_TOKEN=<自己设一个口令> \
python3 mcp/mcp_http_server.py --host 0.0.0.0 --port 18080
```

然后在任何一台机器上（比如你本机）先用 curl 验一下通不通：

```bash
curl -s -H 'Content-Type: application/json' \
     -H 'Authorization: Bearer <上面那个口令>' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
     http://<局域网服务器IP>:18080/mcp
# 预期：返回 9 个工具 arthas_list_pods / status / health_summary /
# dashboard / jvm / thread / monitor / trace / reset；不带口令应该返回 401
```

Agent 客户端里配远程 MCP，见 `mcp/mcp-config-remote.example.json`
（把 URL 里的 IP/端口和口令换成你自己的；不同客户端的远程 MCP
配置格式略有不同，URL 和 Authorization 头这两个要素是不变的）。
流量还是走业务的 NodePort（比如 `http://10.129.73.14:32320/...`），
诊断走这个 MCP HTTP 地址，两条通道分开，客户端机器上全程不需要
kubectl。

注意：这个 HTTP 服务器本身没有集群外的鉴权之外的保护，务必只放
在局域网里（防火墙 / NodePort 只对局域网开放），不要裸暴露到公网，
口令也不要写进仓库。

## 本地接 MCP（Agent 客户端）

把 `mcp/mcp-config.example.json` 里的绝对路径改成你本机的路径，
按你所用客户端的 MCP 配置格式粘贴。MCP Server 是 stdio，
本地跑，不用部署到集群。

MCP 工具（演示版，刻意收窄）：

| 工具 | 用途 |
| --- | --- |
| arthas_list_pods | 列演示 Pod |
| arthas_status | 预检：Pod phase/Ready、as.sh、Java PID、telnet（as.sh 4.0.5 必需）+ `version` 轻量 attach 证明，不跑插桩；诊断失败或演示前先调它 |
| arthas_health_summary | 只读聚合：一次跑 dashboard / jvm / thread，返回堆 / Old 区 / GC 行、线程状态和摘要；单快照不能判泄漏，隔一段时间再调一次、对比堆 / Old 区 |
| arthas_dashboard / arthas_jvm / arthas_thread | 只读快照 |
| arthas_monitor | 方法调用量 / 成功率 / 平均耗时（限 5-30 秒） |
| arthas_trace | 方法调用树（限 1-10 次） |
| arthas_reset | 清插桩，trace/monitor 后必调 |

注意：这两个新工具只用业务镜像里已有的 Arthas 4.0.5 命令
（`version / dashboard / jvm / thread`），不依赖官方实验版
Arthas MCP（4.0.5 里还没有它），所以业务镜像不用升级，
MCP 也不用为官方 MCP 改架构。

没有 ognl、redefine、retransform、heapdump、tt 回放工具，
这是故意的。kind 默认只允许 `demo-arthas`（标签 `app=demo-service`）；
gzdt 用配置里的 `arthas-demo-gzdt` 一项（命名空间 `gzdt`、
标签 `app=arthas-demo-service`）。
环境变量 `ARTHAS_ALLOWED_NAMESPACES` 可以改，但不要改成生产命名空间。

## 如果不用本地 kind，线上 k8s 有哪些选择

先说结论：**演示首选本地 kind**，线上只在“需要给别人一个长期可
访问的地址”时再考虑。下面是按“能不能真的跑 kubectl exec +
Arthas attach”来分的，不是按“能不能跑容器”来分的。

### 能直接当 k8s 演示环境的

- **本地 kind / minikube / k3s / Rancher Desktop**：免费、语义最真、
  kubectl exec 直接可用、随时重建。缺点是只在你本机，分享时要投屏。
- **Oracle OKE（Always Free）**：你已有 Oracle Cloud 账号，这是最
  顺的一条线上路。Basic 集群控制面免费，节点可用 Always Free 的
  Ampere A1 额度；但注意 2026 年 6 月后额度已从 4 OCPU / 24GB
  减半到约 2 OCPU / 12GB，镜像必须是 arm64 才能跑在 A1 节点上，
  且闲置实例可能被回收。演示前先确认你的租户还有没有剩余额度。
- **GKE / AKS 免费层**：控制面约免费或有抵扣（GKE 每月约
  $74.40 额度可覆盖一个单区集群，AKS Free 层控制面 $0 但无 SLA），
  但节点、负载均衡、流量还是要钱，演示完记得删集群，别长期挂着。
  适合“公司已有对应云账号”的情况，不适合只为一次分享新开账号。
- **临时在线实验场 Killercoda / Play with Kubernetes**：浏览器里
  就能开临时集群，适合练 kubectl，但会话有时限（通常 1-4 小时量级）、
  环境会回收、装 Arthas 和自定义镜像比较折腾，**不适合作为正式分享
  的主环境**，只适合你自己提前练手。

### 不能直接当 k8s 演示环境的（容易踩坑）

- **Railway**：能跑 Dockerfile / 预构建镜像，但它不是 Kubernetes，
  没有 Pod、没有 kubectl exec、没有命名空间概念。同一份镜像能在
  Railway 上跑起来，不等于 Arthas MCP 那套“按服务找 Pod 再 exec”
  能复用。只能用来演示“Java 服务本身”，不能用来演示“k8s 线上排查”。
- **Cloudflare Workers / Containers**：Workers 本来就不是 Pod 模型；
  Containers 是按 Worker 驱动的按需容器，没有 kubectl、没有
  Deployment / Pod 语义，Arthas attach 那条路走不通。同样只能跑
  服务，不能复现 k8s 排查流程。

一句话：要演示 k8s 排查，就必须真有一个能 `kubectl exec` 的集群；
Railway、Cloudflare 解决的是“把容器跑起来”，不是“k8s 排查”。

## 彩排前检查单

- [ ] kind 集群、镜像、部署、冒烟、Arthas 直连五步全跑通
- [ ] gzdt 部署用的是 `k8s/gzdt/` 两个文件，不是 `k8s/` 的 kind 文件
- [ ] 慢接口三种模式的 trace 证据都截过图（nplus1 看次数、
      downstream_slow 看单次、self_slow 看自身）
- [ ] 内存泄漏开关能开能关，reset 后 Old 区回落或 Pod 重建后恢复
- [ ] MCP 配置里的路径、命名空间、kubectl 上下文都改成本机的
- [ ] 兜底：现场 Arthas 失败时切录好的 trace / dashboard 截图，
      不要现场排环境
- [ ] 演示命名空间、故障开关、MCP 工具都没有指向生产
