# CSI201 Project 1
Networked Quiz Tournament

## 一次运行全部测试

在 VS Code 中选择“终端 → 新建终端”，确认位于项目根目录，然后运行：

```sh
python run_tests.py
```

Linux / 官方 Docker 中使用 `python3 run_tests.py`，或在已启动的课程容器中执行
`docker exec csi201-cw1-dev python run_tests.py`。
需要 Python 3.12 标准库，不需要 pip 安装。测试自动启动和清理临时服务端及客户端，不需要手动先启动服务器。
两组都显示 `OK` 且命令退出码为 0 才算通过；故意测试非法参数时显示的 error 文本是预期输出。
Windows 跳过 Linux SIGINT 验收测试，显示 `skipped=1`；该项必须在官方 Docker 环境再运行。
真实十客户端测试覆盖两个房间第一题及房主进程意外退出；完整十题流程另由双客户端配对测试覆盖。
测试过程约一分钟，具体取决于电脑性能。Docker 尚未验证时，不得将本机结果写作官方环境证据。

## 服务端开发

Python 3.12，仅使用标准库。双方接口见 [PROTOCOL.md](PROTOCOL.md)。

```sh
python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
python3 -m unittest discover -s server/tests -v
```

启动成功输出 `SERVER_READY`；题库或参数非法时非零退出，不监听。Ctrl+C 关闭所有连接。
同机连接使用 127.0.0.1；需要接受其他主机连接时绑定 0.0.0.0，并让客户端连接实际服务器地址。

## 客户端开发

`client.py` 是对应的命令行客户端，同样只用标准库和原始 TCP socket。客户端不读题库、不判定答案：
房间状态、计时、判分和排名全部由服务端决定，它只负责登录、发送请求、展示服务端推送的事件。

```sh
python3 client.py --server 127.0.0.1 --port 5000 --id student1 --name Alice
python3 -m unittest discover -s client/tests -v
```

进入后输入 `/help` 查看全部命令：`/quizzes`、`/rooms`、`/create <quiz_id>`、`/join <room_code>`、
`/players`、`/ready`、`/unready`、`/start`、`/answer <A|B|C|D>`、`/score`、`/leave`、`/quit`。
接收线程独立于输入，事件可以在任何时候打印；stdout 被重定向时不打印提示符，演示录屏/日志更干净。

- `client.py`：长度前缀分帧（逐字节、粘包、跨读都能正确重组）、请求 ID 与响应匹配、事件渲染、
  断线/协议不兼容处理、Ctrl+C 直接关连接（由服务端按掉线处理）。
- `client/tests/test_client.py`：单元测试，覆盖分帧、信封校验、错误码、房间与答题状态、
  排行榜与中文列宽；不打开 socket，毫秒级完成。
- `client/tests/test_integration.py`：启动真实 `server.py` 与两个真实客户端进程，跑完整 10 题比赛，
  并覆盖重复登录被拒、未准备不能开局、结束后不能再答等配对场景。

## 教师 Docker 环境

保持老师提供的 Dockerfile 和 compose.yaml 不变。在项目目录构建并启动一个共享容器：

```sh
docker compose build
docker compose run -d --name csi201-cw1-dev python -c "import time; time.sleep(86400)"
docker exec -it csi201-cw1-dev python server.py --host 0.0.0.0 --port 5000 --questions questions.json
```

另开终端，在同一容器运行测试或客户端：

```sh
docker exec csi201-cw1-dev python -m unittest discover -s server/tests -v
docker exec csi201-cw1-dev python -m unittest discover -s client/tests -v
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
- `client.py`：客户端全部实现，单文件、零依赖，可直接 `python3 client.py ...` 运行。
- `client/tests/test_client.py`、`client/tests/test_integration.py`：客户端单元测试与真实进程配对测试。

没有数据库、重连恢复、加密或账户密码；会话仅绑定本次 TCP 连接，适用于作业受控网络。
排行榜按分数降序、用户 ID 升序；最少两人、全员准备、房主开始；详见协议。
本机执行只算开发检查，Docker 验收、双端配对测试及 Ctrl+C 演示仍需在官方环境实际完成并记录。
