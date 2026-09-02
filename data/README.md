# 数据集

**大数据集放外置盘，项目里只留符号链接。**

| 数据集 | 位置 | 大小 | 用途 |
|---|---|---|---|
| Northwind | `data/northwind/`（项目内） | 342K | CI regression |
| **Olist** | 符号链接 → 外置盘 | 126MB | 主 Demo（真实脏数据） |
| Home Credit | 未下载 | 2.68GB | R5 压力测试 |
| exports | `data/exports/`（项目内） | 小 | 接入路径等价性测试的导出文件 |

外置盘路径：`/Volumes/kong disk 727899339/datalaker-datasets/`

## ⚠️ 外置盘掉线时

Mac 睡眠后外置盘会断开，此时：

- `data/olist` 符号链接**指向不存在的路径**
- Docker 也会连带出问题（数据盘在同一块盘上）

判断方法：

```bash
ls -lh data/olist/          # 断了会报 No such file or directory
```

处理：插回盘即可，符号链接自动恢复。**不要删链接重建。**

## Kaggle

凭证在 `~/.kaggle/kaggle.json`（0600）。新版 token 只需 `key`，无需 username。

```bash
.venv/bin/python -c "
import kaggle; kaggle.api.authenticate()
kaggle.api.dataset_download_files('olistbr/brazilian-ecommerce',
    path='/Volumes/kong disk 727899339/datalaker-datasets/olist', unzip=True)"
```

Home Credit（R5 再下）：

```bash
kaggle competitions download -c home-credit-default-risk -p <外置盘路径>
```
