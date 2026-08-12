# Research notes

此目录保存上游接口变化的排查资料，不参与主程序运行。

- `PROJECT_STATUS.md`：基础评定和拓展评定接口的历史验证记录。
- `decode_eapi_har.py`：从本地 HAR 抓包中筛选并检查 EAPI 请求的辅助脚本。

HAR 文件可能包含 Cookie、请求头、用户标识和其他账号信息，已由 `.gitignore` 排除。使用解码脚本前请在本地保存抓包，不要将原始 HAR 上传到 Issue 或公开仓库。
