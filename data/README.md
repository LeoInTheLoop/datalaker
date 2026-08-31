# 验证数据集

按 readme 第 16.4 节分层。**数据文件已 gitignore，只有本说明入库。**

## 放哪里

| 数据集 | 大小 | 建议位置 |
|---|---|---|
| Northwind | 342K | 项目内 `data/`（默认） |
| Olist | ~45MB | 项目内 `data/` |
| Home Credit | 2.68GB | **项目外**，用 `.env` 的 `DATASETS_DIR` 指向 |

理由：git 不适合存数据一律不入库；小的放项目内省事；
大的放项目内会撑大 docker build context 和备份。

> ⚠️ 若把 `DATASETS_DIR` 指到外置盘，注意 Mac 睡眠后外置盘会掉线——
> 这台机器的 Docker 数据盘就在外置盘上，踩过一次。

| 数据集 | 规模 | 用途 | 获取 |
|---|---|---|---|
| Northwind | 7 表，极小 | CI regression，每次改动几分钟跑完 | 公开，无需认证 |
| Olist | 9 表 / 10 万订单 | 主 Demo | Kaggle，需 API key |
| Home Credit | 10 文件 / 2.68GB | 压力测试，证明非 hard-code | Kaggle，需 API key |

## Kaggle 认证

kaggle.com → Account → Create New API Token → 下载 `kaggle.json` 到 `~/.kaggle/`，
然后 `chmod 600 ~/.kaggle/kaggle.json`。

```bash
pip install kaggle
kaggle datasets download -d olistbr/brazilian-ecommerce -p data/olist --unzip
kaggle competitions download -c home-credit-default-risk -p data/homecredit
```
