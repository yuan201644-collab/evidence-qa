# release —— 离线交付包

本目录只放**说明文件**，不放包本身。`.gitignore` 里挡掉了 `release/*.zip` 与
`release/*.tar.gz` —— 375MB 的二进制包进 git 会让仓库体积永久膨胀，
之后每次 clone 都要背这个开销。

## 包是什么

| 项 | 值 |
|---|---|
| 文件名 | `ict-db-images-v1.0.0.tar.gz` |
| 内容 | 两个镜像：`pgvector/pgvector:pg16` + `chriseaton/adventureworks:postgres` |
| 实测字节数 | 392,519,074 字节（约 374 MiB，口语说 375MB） |
| 打包耗时 | `docker save` 20 秒 |
| 载入耗时 | `docker load` 19 秒，恢复出两个 tag |
| 包内未压缩 tar 的大小 | 383MB |

体积为什么比预想小：`docker images` 报的 621MB + 1.02GB 是**解压后**的体积，
而 tar 里装的是已经压缩过的层 blob。按 `docker images` 的数字估算会高估约 4 倍。

## 为什么要它

本机实测 Docker Hub 直连不可用（`registry-1.docker.io` 连接超时），
两个镜像都是经加速地址拉取后打回原 tag 的。若评委机器也连不上外网，
没有这个包就起不了库 —— 而「一条命令起 Demo」是演示环节的前提。

## 怎么用（目标机，无需外网）

```bash
docker load -i release/ict-db-images-v1.0.0.tar.gz
docker compose -f infra/docker-compose.yml up -d
python infra/scripts/verify_infra.py
```

载入后 `docker images` 应能看到两个 tag。compose 里镜像是 tag + digest 双写的，
载入的镜像 digest 一致时不会触发联网拉取。

## 怎么重新生成（包丢了也能重建）

需要有 Docker 的机器，且两个镜像已在本地：

```bash
docker save pgvector/pgvector:pg16 chriseaton/adventureworks:postgres \
  | gzip -9 > release/ict-db-images-v1.0.0.tar.gz

# 验证包可用（会在本机重新载入一遍）
docker load -i release/ict-db-images-v1.0.0.tar.gz
```

本地若没有这两个镜像，先按 `infra/README.md` 的「镜像拉不动的备选通道」
经加速地址拉取，再用 `docker tag` 打回原 tag。

## 交付渠道（未定，待负责人确认）

这个包怎么给评委还没定，三个候选：

| 方案 | 说明 | 前提 |
|---|---|---|
| 随交付物一起提交 | 最稳，评委拿到即可跑 | 提交通道允许 375MB 附件 |
| 单独介质（网盘 / U 盘） | 不受通道限制 | 交付文档要写清提取方式与完整性校验方式 |
| 不打包，依赖加速地址 | 最省事 | 评委机器能访问加速地址 |

倾向「随交付物提交为默认，加速地址写进交付说明兜底」。
最终选哪条取决于初赛提交通道的附件大小上限。
