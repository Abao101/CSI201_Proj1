# CSI201 Project 1
Networked Quiz Tournament

## 服务端开发

Python 3.12，仅使用标准库。双方接口见 [PROTOCOL.md](PROTOCOL.md)。
客户端由另一位组员实现，当前仓库的 tests 使用测试连接验证服务端，不能替代最终客户端联调。

```sh
python3 server.py --host 127.0.0.1 --port 5000 --questions questions_50.json
python3 -m unittest discover -s server/tests -v
```

启动成功输出 `SERVER_READY`；题库或参数非法时非零退出，不监听。Ctrl+C 关闭所有连接。
同机连接使用 127.0.0.1；需要接受其他主机连接时绑定 0.0.0.0，并让客户端连接实际服务器地址。

## 教师 Docker 环境

保持老师提供的 Dockerfile 和 compose.yaml 不变。在项目目录构建并启动一个共享容器：

```sh
docker compose build
docker compose run -d --name csi201-cw1-dev python -c "import time; time.sleep(86400)"
docker exec -it csi201-cw1-dev python server.py --host 0.0.0.0 --port 5000 --questions questions_50.json
```

另开终端，在同一容器运行测试或客户端（客户端完成后）：

```sh
docker exec csi201-cw1-dev python -m unittest discover -s server/tests -v
docker exec -it csi201-cw1-dev python client.py --server 127.0.0.1 --port 5000 --id student1 --name Alice
```

结束后：`docker stop csi201-cw1-dev`，然后 `docker rm csi201-cw1-dev`。
测试自行使用临时端口，不影响已启动的服务端。此方式让两端均在官方环境运行，不需要修改 compose 的端口映射。
正式提交时将题库命名为 questions.json，按任务书整理 Codes/ 和 Documentation/；不要把 Dockerfile、镜像或缓存打包提交。

## 模块与设计

服务端实现和测试统一放在 `server/` 目录。根目录的 `server.py` 仅为启动入口，保留任务书规定的启动命令。
题库、协议文档和 Docker 配置保留在根目录，供整个项目使用。`__pycache__/` 是可再生的 Python 缓存，已由 `.gitignore` 排除，不提交到 Git。

- `server/protocol.py`：双方可共享的长度前缀编解码和严格 JSON 解析。
- `server/question_bank.py`：全量题库校验，1 MB 按 1048576 字节解释；visible 字段采用可打印字符且非空字段不能全为空白。
- `server/server.py`：selectors 单线程事件循环，独占用户/房间/计时/分数状态；非阻塞发送队列和单调时钟。
- `server/tests/test_server.py`：分帧、题库、真实 TCP 比赛流程、超时、双房间并发和离线行为。

没有数据库、重连恢复、加密或账户密码；会话仅绑定本次 TCP 连接，适用于作业受控网络。
排行榜按分数降序、用户 ID 升序；最少两人、全员准备、房主开始；详见协议。
本机执行只算开发检查，Docker 验收、最终客户端配对测试及 Ctrl+C 演示仍需实际完成并记录。
