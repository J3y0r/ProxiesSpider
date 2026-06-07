# FreeProxies

用于抓取并分流网络上免费的代理列表。

## 运行

```bash
python scrape_freevpnnode.py
```

## 输出

脚本会在 `output/` 目录下生成 6 个文件：

- `http_no_auth.txt`
- `http_auth.txt`
- `socks4_no_auth.txt`
- `socks4_auth.txt`
- `socks5_no_auth.txt`
- `socks5_auth.txt`

格式：

- Raw：`ip:port`
- Normal: `http|socks4|socks5://ip:port`
