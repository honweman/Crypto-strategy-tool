# 部署说明

## 本机临时公网演示

当前最方便的手机访问方式是 Cloudflare quick tunnel。它适合演示，但不保证长期在线，电脑睡眠、终端关闭或进程退出后公网 URL 会失效。

```bash
./scripts/start_public_demo.sh
```

启动后终端会打印一个 `https://*.trycloudflare.com` 地址，用手机打开即可。

可选环境变量：

```bash
CRYPTO_LAB_SECRET_KEY="replace-with-a-long-random-secret" \
CRYPTO_LAB_ADMIN_EMAIL="admin@example.com" \
CRYPTO_LAB_ADMIN_PASSWORD="change-me-now" \
./scripts/start_public_demo.sh
```

## 固定公网访问

要给其他人长期使用，不建议继续用 quick tunnel。推荐：

1. 准备一个域名，例如 `crypto.example.com`。
2. 在 Cloudflare 账号里创建 named tunnel。
3. 把域名 CNAME 到 tunnel。
4. 把 `crypto-lab-web` 和 `cloudflared` 配成系统服务。
5. 设置强随机 `CRYPTO_LAB_SECRET_KEY`，并开启 HTTPS cookie：

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
