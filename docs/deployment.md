# 部署说明

## 本机临时公网演示

手机临时访问可使用 [Cloudflare quick tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。它适合演示，不保证长期在线，电脑睡眠、断网或进程退出后公网 URL 会失效；每次重新创建的地址不同。

启动前需要已有数据库、修改过默认密码的账户，以及 `output/public-runtime.json` 私密配置（权限必须为 `600`）：

```json
{
  "db_path": "/absolute/path/to/data/crypto_lab.sqlite3",
  "secret_key": "USE_A_CRYPTOGRAPHICALLY_RANDOM_SECRET_OF_AT_LEAST_40_CHARACTERS",
  "source_cooldowns": {}
}
```

`secret_key` 必须与已有 API 凭据的加密密钥匹配。已有凭据时不能直接换密钥；需要先做备份和重加密迁移。配置和密码不得提交 Git。本次实际部署的密钥由程序随机生成，保存在被忽略的 `output/` 目录中。

```bash
./scripts/start_public_demo.sh
```

启动后终端会打印一个 `https://*.trycloudflare.com` 地址，用手机打开即可。

脚本只监听 `127.0.0.1`，通过隧道提供 HTTPS；会拒绝默认密码、过短密钥、可被其他系统用户读取的配置及无法解密的 API 凭据。启用 Secure/HttpOnly/SameSite Cookie、同源写请求检查、登录全局每分钟 30 次上限，并关闭实盘下单。这个限流适用于单进程临时演示，生产部署需要边缘防护及分布式限流。

可选启动参数：

```bash
CRYPTO_LAB_PUBLIC_CONFIG="/private/path/to/public-runtime.json" \
CRYPTO_LAB_PORT=8093 \
./scripts/start_public_demo.sh
```

## 固定公网访问

要给其他人长期使用，不建议继续用 quick tunnel。推荐：

1. 准备一个域名，例如 `crypto.example.com`。
2. 在 Cloudflare 账号里创建 named tunnel。
3. 把域名 CNAME 到 tunnel。
4. 把 `crypto-lab-web` 和 `cloudflared` 配成系统服务。
5. 使用独立的生产配置、强随机加密密钥、强密码/多因素认证、自动备份及访问审计，并开启 HTTPS cookie：

```bash
CRYPTO_LAB_COOKIE_SECURE=1
```

## 实盘前要求

实盘默认关闭，需要同时满足：

- 环境变量 `CRYPTO_LAB_ENABLE_LIVE_TRADING=1`
- 用户拥有 `trading:execute_live` 权限
- 配置页 `交易风控开关` 中开启实盘交易
- 后端交易所 connector 已实现

当前版本仍返回 `exchange_connector_not_configured`，不会真实下单。
