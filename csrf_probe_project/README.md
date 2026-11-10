# CSRF 反向代理排查工程（最小可复现）

应用搬到 HTTPS 反向代理后登录表单偶发 CSRF 403。本工程**不关闭 CSRF、不放开任意来源**，
而是用 Django 自带的测试客户端，从 **WSGI（`WSGIRequest`）和 ASGI（`ASGIRequest`）两个
真实请求构造入口**一路走到未修改的 `CsrfViewMiddleware`，把每一步判定都打印出来，从而定位
"偶发" 到底发生在哪条分支。

所有场景复用**同一份固定且合法**的 CSRF cookie（32 位 secret）与表单 token（由该 secret
掩码得到，见 `probe/harness.py` 的 `CSRF_SECRET` / `CSRF_FORM_TOKEN`），因此任何 DENY 都
不是 token 错误，而只可能来自来源 / 协议 / 主机判定。

## 运行

```bash
# 打印 16 个场景的完整判定路径（无需起服务、无需真实代理）
python manage.py

# 运行 17 个锁定契约的断言
python manage.py test probe -v 2
```

## 实际判定路径（基于当前代码，非文档推测）

对非安全方法（POST 等），`CsrfViewMiddleware.process_view()` 的顺序是
（`django/middleware/csrf.py`）：

1. `request.is_secure()` —— 实际是 `request.scheme == "https"`。
   `scheme` 先看 `SECURE_PROXY_SSL_HEADER` 指向的头；否则回退到入口自身：
   - WSGI：`WSGIRequest._get_scheme()` 读 environ 的 `wsgi.url_scheme`；
   - ASGI：`ASGIRequest._get_scheme()` 读 `scope["scheme"]`，再回退 `"http"`。
2. 带 `Origin` 头 → `_origin_verified()`，依次尝试三条规则：
   - 自身来源：`"<https|http>://" + request.get_host()` 与 Origin 完全相等；
   - `CSRF_TRUSTED_ORIGINS` 中的**精确**条目（`allowed_origins_exact`）；
   - 通配条目，按 **scheme 分组**后用 `is_same_domain()` 判子域
     （`allowed_origin_subdomains`）。
   三条都不中 → `REASON_BAD_ORIGIN`。
3. 没有 `Origin` 但 `is_secure()` 为真 → `_check_referer()` 做严格 Referer 校验：
   无 Referer / 非 https / 主机不匹配分别给出对应 REASON。
   （非安全请求没有 Origin 时**跳过** Referer 分支。）
4. 最后 `_check_token()` 比对 cookie secret 与表单/头 token。本工程里这一步恒通过。

`get_host()` 的取值（`django/http/request.py`）：
`USE_X_FORWARDED_HOST` 为真才用 `X-Forwarded-Host`，否则用 `Host`，再回退
`SERVER_NAME:SERVER_PORT`；取到后仍要过 `ALLOWED_HOSTS` 校验，失败抛 `DisallowedHost`。

## 一行配置差异的成功 / 失败对照

`probe/matrix.py` 里每个场景只改一个设置或一个头。核心对照：

| 对照 | 失败侧（一行差异） | 成功侧 | 拒绝原因（失败侧） |
| --- | --- | --- | --- |
| A1/A2 | `SECURE_PROXY_SSL_HEADER=None`（默认） | `= ("HTTP_X_FORWARDED_PROTO","https")` | Origin 被当作 http 自身来源，`BAD_ORIGIN` |
| A3 | 可信头被客户端伪造成 `X-Forwarded-Proto: http` | 代理覆写为 `https` | `is_secure()=False` → `BAD_ORIGIN` |
| B1 | 安全 POST 无 Origin 且无 Referer | 浏览器/策略保留 Referer | `REASON_NO_REFERER` |
| B2 | Referer 被降级成 `http://` | 保持 `https://` | `REASON_INSECURE_REFERER` |
| C1/C2 | Origin 带 `:8443`、Host 被改写成 443 | 加一条 `CSRF_TRUSTED_ORIGINS` 含 `:8443` | 端口不一致 → `BAD_ORIGIN` |
| D1/D2 | `https://evil-app.example.com` 相似后缀 | 真实子域 `login.app.example.com` | `is_same_domain` 正确拒绝相似后缀 |
| E1/E2 | `USE_X_FORWARDED_HOST=True` 且头未清洗 | 保持 False 或代理清洗 | 伪造主机先被 `ALLOWED_HOSTS` 拦截 |

"偶发" 的根因通常是 **A/B 两组**：不同浏览器对 POST 是否带 `Origin` 行为不同，而内部跳是
明文 http。Chrome 风格带 `Origin: https://...` 时，因 `is_secure()=False` 自身来源算成
`http://` 而失败；Firefox 风格只带 Referer 时，又会落入严格 https Referer 分支。

## 修复分层：代理清洗 vs Django 设置

**应由入口代理负责（清洗 / 覆写转发头，Django 无法替你保证头的真实性）：**
- 终止 TLS 后**覆写**（而非追加）`X-Forwarded-Proto: https`，并剥掉客户端自带的同名头，
  防止 A3 的伪造值。
- 清洗 `X-Forwarded-Host` / `X-Forwarded-Port`，只在代理保证可信时再让 Django 消费。
- 保证对外 `Host:端口` 与浏览器 `Origin` 一致，避免 C 组端口漂移；保留 https 的 Referer。
- ASGI 入口由服务器把 TLS 映射进 `scope["scheme"]="https"`（见 A4），不依赖转发头。

**应由 Django 设置负责（声明信任，且必须精确，不能用通配兜底）：**
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`：让 `is_secure()` 认
  代理协议（前提是代理已清洗该头）。
- 跨子域用 `CSRF_TRUSTED_ORIGINS = ["https://*.app.example.com"]`；非默认端口必须写全
  `https://app.example.com:8443`。scheme 是规则的一部分，http 来源不能借 https 通配。
- 确需消费转发主机时才开 `USE_X_FORWARDED_HOST=True`，并确保对应域在 `ALLOWED_HOSTS`。

## 不该被信任的转发头（工程内有专门场景）

- 未配置 `SECURE_PROXY_SSL_HEADER` 时，`X-Forwarded-Proto` 只是普通字节，不影响
  `is_secure()`（A2、WSGI 下划线头用例）。
- ASGI 下带下划线的头名（如 `x_forwarded_proto`）会被 `ASGIRequest.__init__` 直接丢弃
  （防混淆伪造），用例 `test_asgi_underscore_header_dropped_by_request_parser` 锁定该行为。
- `USE_X_FORWARDED_HOST=False`（默认）时 `X-Forwarded-Host` 不参与主机判定（E1）。

## 是否需要改框架代码

不需要。所有拒绝都与 `CsrfViewMiddleware`、`HttpRequest.scheme/get_host`、
`is_same_domain()` 的既有契约一致；相似后缀、scheme 隔离、端口精确匹配均按预期工作。
本工程只通过**子类化中间件观察其私有方法**记录分支，未修改 `django/` 下任何文件。
