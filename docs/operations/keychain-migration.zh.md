# 把通知凭据迁入钥匙串

两个真正的通知 secret —— 飞书 app secret 与 Telegram bot token —— 可以改从独立的
macOS 钥匙串读取，再经文件描述符交给需要它们的进程，而不再经过环境变量。

**这是可选开启、且默认关闭的功能。** 不做任何改动的部署，行为与改造前完全一致。在
假定这层保护已经生效之前请先读完本页：机制「可用」和机制「在用」是两件事，只有后者
才改变同一台机器上的攻击者能从进程列表里读到什么。

## 改变了什么，没改变什么

索引键仍然留在 `backend/.env` —— `FEISHU_APP_ID` 与 `TELEGRAM_CHAT_ID` 是配置而不是
凭据，把它们一并迁进去只会让非机密配置平白进入凭据管理，没有收益。迁移的只有两个
secret。

| | 改造前 | 改造后 |
|---|---|---|
| 索引键 | `backend/.env` | `backend/.env`（不变） |
| secret | `backend/.env` → `os.environ` | 钥匙串 → 管道 → 环境变量里只有 fd 号 |
| `ps eww <pid>` 能看到 secret | 能 | 不能 |
| Linux / Windows | 不变 | 不变 |

开关是 `PDT_DISABLE_KEYCHAIN_SECRETS`，名字是**故意反着**的：它是「禁用」钥匙串的，
所以变量**不存在**或取 `0` / `false` 时钥匙串才生效。其他任何写法 —— `true`、`1`、
`yes`、拼错、多一个空格 —— 都保持关闭。反过来读会导致每次查找都去 shell 出去问一个
部署还没填好的凭据库，失败方式只有运维自己能排查；而猜错方向的代价不过是 secret 在
环境变量里多待一会儿，那本来就是安装当前的既有状态。

## 开始之前

你需要在 macOS 上。其他任何平台都不会去查钥匙串，也就没有可迁移的东西 —— 开关根本
不会被读取。

先确认部署当前凭据是好的，因为下面每一步的验收标准都是「通知仍然发得出去」，而如果
本来就发不出去，这个检查毫无意义：

```bash
backend/.venv/bin/python3 -m backend.cli secrets verify
```

每一行都必须是 `source=os.environ`，且没有任何一行是 `source=missing`。

## 1. 建独立钥匙串

provider 读的是它自己的钥匙串，不是你的 login keychain —— 它要碰的条目不会混进
login keychain 里日积月累的那几百条里。

```bash
security create-keychain -p "runtime-secrets" ~/Library/Keychains/runtime-secrets.keychain-db
security unlock-keychain -p "runtime-secrets" ~/Library/Keychains/runtime-secrets.keychain-db
security set-keychain-settings -lut 21600 ~/Library/Keychains/runtime-secrets.keychain-db
```

第三行设置锁定超时；不设的话钥匙串会按它自己的默认时长锁上，之后每次通知投递都会先
卡在一个没人应答的授权提示上。

## 2. 写入两个 secret

account 是**索引键的值**，既不是 secret 本身也不是 service 名。provider 只按 account
定位、从不传 `-s`，所以 account 必须是 `.env` 里已经有的那个东西。

```bash
# 从 .env 读索引值，全程不回显
FEISHU_APP_ID=$(grep -m1 '^FEISHU_APP_ID=' backend/.env | cut -d= -f2-)
TELEGRAM_CHAT_ID=$(grep -m1 '^TELEGRAM_CHAT_ID=' backend/.env | cut -d= -f2-)

security add-generic-password -U -a "$FEISHU_APP_ID"   -w "$(grep -m1 '^FEISHU_APP_SECRET=' backend/.env | cut -d= -f2-)" ~/Library/Keychains/runtime-secrets.keychain-db
security add-generic-password -U -a "$TELEGRAM_CHAT_ID" -w "$(grep -m1 '^TELEGRAM_BOT_TOKEN=' backend/.env | cut -d= -f2-)" ~/Library/Keychains/runtime-secrets.keychain-db
```

`-U` 表示存在即更新，所以凭据轮换之后重跑这条是安全的。

继续之前先验证。两行都必须是 `source=keychain`：

```bash
PDT_DISABLE_KEYCHAIN_SECRETS=0 backend/.venv/bin/python3 -m backend.cli secrets verify
```

!!! warning "出现交互式授权提示，意味着非交互读者读不出来"
    `security find-generic-password -w` 在条目的访问控制要求授权、而调用进程无法提供
    授权时，会返回退出码 128 且没有任何输出。服务端进程没有终端可以应答提示，因此
    一个以「每次读取都要批准」方式创建的条目，会让每一次通知投递都静默地解析失败。

    `secrets verify` 把这种情况报成 `source=missing` 而不是报错，这就是要认出的症状。
    查条目的访问控制（`security dump-keychain <path>` 会列出它），放宽到允许读取的
    应用，或者用 `-T` 指明需要的二进制重新创建该条目。

## 3. 打开开关

```bash
echo 'PDT_DISABLE_KEYCHAIN_SECRETS=0' >> backend/.env
```

然后把两个 secret 从 `.env` 里删掉。留着它们系统不会替你报错：钥匙串启用时它们只是
被忽略，而等到开关被关回去的那一天，它们早就过期了。

```bash
# 在通知确认可用之前，先在仓库外留一份
cp backend/.env "$HOME/.pdt-env-backup"
```

## 4. 确认

重启服务，然后确认通知真的发出去了。provider 每个进程只解析一次 secret，成功和失败
都进缓存，所以在跑着的进程会一直沿用它第一次的判断 —— 在活进程底下打开开关，在重启
之前不会有任何变化。

```bash
backend/.venv/bin/python3 -m backend.cli secrets verify   # 两行都是 source=keychain
```

然后发一条东西。状态端点会按通道报告来源而不打印凭据：

```bash
curl -s -H "X-PDT-Request: 1" http://127.0.0.1:8000/api/notifications/status
```

## 回滚

钥匙串路径是叠加式的，所以回滚就是删掉一行：

```bash
sed -i '' '/^PDT_DISABLE_KEYCHAIN_SECRETS=/d' backend/.env
cp "$HOME/.pdt-env-backup" backend/.env
```

钥匙串和它的条目会留在原处。删除钥匙串（`security delete-keychain <path>`）是另一件
不可逆的事，所以不属于回滚步骤 —— 只在确定不会再打开开关之后再做。

## 轮换凭据

先把新值写进钥匙串并确认，再删掉 `.env` 里的旧副本（如果还有）。钥匙串启用期间根本
不读 `.env`，所以只更新钥匙串的轮换就是完整的；两边都更新的轮换不是 —— 因为开关一旦
被关回去，用的就是 `.env` 里那份。

## 新增第三个 secret

`backend/credentials.py` 里是注册表 —— 每个 secret 一条 `SecretSpec`，写明回退时读
哪个环境变量、以及哪个环境变量携带钥匙串 account。加上这一行之后，禁止从环境变量读
secret 的静态门禁下一次运行就会带上它，因为那份清单是从注册表推导出来的，不是抄一份
副本。`secrets` 子命令同样遍历这个注册表，所以它们无需再改就会报出新 secret。

## 这防不住什么

读钥匙串仍然需要以你的身份运行。本地以你的账号运行的进程能读任何你能读的东西，钥匙串
提高的是**意外**场景的成本 —— 进程列表、崩溃转储、一行日志 —— 而不是提高一个决心要
拿到它的人的成本。完整信任模型见 [Security](../security.md)。
