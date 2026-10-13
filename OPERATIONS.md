# 容器部署与备份恢复（M12 / 0.12.0）

## 首次部署或升级

Linux服务器需要Docker Engine、Compose插件和可访问的固定镜像/依赖/模型下载源。普通API、worker及Guest不使用宿主Python/Node环境；当前实际验收是Windows Docker Desktop中的Linux/x86_64引擎，独立物理Linux与ARM未实测。

```sh
sh scripts/init-env.sh
# 编辑.env，填写主/备用/审核模型和公开搜索API配置。
sh scripts/deploy.sh
```

部署脚本先构建API，然后构建维护工具与预备Guest镜像；启动存储与固定CPU模型，显式执行迁移和幂等种子，再启用production前端。已有结构只能追加迁移，不能修改旧迁移。首次构建需要下载，不承诺离线冷启动；之后可保留镜像和模型卷复用。

web默认绑定127.0.0.1，服务器远程访问通过反向代理/TLS，HTTPS部署应设置`SESSION_COOKIE_SECURE=true`。维护与存储端口不直接向公网开放。日常前端仍使用本地`npm run dev`，生产验收才启动web容器。

Runner持有专用Docker引擎socket，API/worker和Guest均不持有；Guest逐任务创建/销毁、断网、无应用凭证、输入只读并有CPU/内存/进程配额。普通容器共享内核，没有gVisor。当前多副本调度限同一Docker引擎，跨宿主属于后续设计。

## 维护窗口备份

先完成正在执行的业务。脚本依次停止worker、Runner、API，保留PG/MinIO；导出PG17自定义档案及整个应用文件Bucket，保存表行数、PG档案和每个对象的SHA清单；结束或失败后恢复原先运行的服务。不会导出.env、模型API Key、Redis或Milvus物理卷。

维护工具是单独的一次性镜像，API/worker不持有PG/MinIO管理员凭证。维护镜像依赖已构建的同版本API镜像：

```sh
docker compose build api
docker compose --profile tools build maintenance
sh scripts/backup.sh backups/20261013
```

Windows：

```powershell
./scripts/backup.ps1 -Directory backups/20261013
```

备份目录必须为空；未完成的备份不覆盖旧备份。只有`manifest.json`生成且validate通过才视为完整：

```sh
MAINTENANCE_DIR=./backups/20261013 docker compose --profile tools run --rm --no-deps maintenance validate
```

备份含业务正文、密码哈希和审计，放在受控磁盘，按部署要求另行加密及异地保留。Linux使用严格umask，Windows依靠本地ACL。SHA证明完整性，不证明外部备份可信；仅恢复本系统生成且可信的PG档案。

## 全新目标恢复

```sh
sh scripts/restore.sh backups/20261013 zhixing_restore_20261013 application-restore-20261013
```

```powershell
./scripts/restore.ps1 -Directory backups/20261013 -Database zhixing_restore_20261013 -Bucket application-restore-20261013
```

目标库名使用小写字母/数字/下划线且最多38字符；Bucket使用小写字母/数字/连字符。不得和源目标相同，已有库/Bucket一律拒绝覆盖。先核对所有SHA，再创建新库和新Bucket。PG原始表数量在任何恢复状态调整前核对；每个恢复文件重新读取验证SHA。

新Bucket创建独立最小权限应用身份，不扩大原应用凭证权限。所有文档的可变集合指针（含已失效/删除中）先改到新集合，防止清理作业访问源索引。Milvus只重建仍有效的知识/私人切片到独立集合，使用备份时固定的真实CPU模型空间；模型空间不同则停止。Redis使用新数据库命名空间从PG重建，不清空源Redis。

旧登录全部失效；未完成run标中断，未消费SQL批准失效，旧沙箱任务标未知，不重复执行。原消息/代码/确认/产物/审计保留。恢复配置使用新的沙箱state/input卷，旧Guest状态不带入新目标；新库旧额度释放不能当作原宿主旧容器已清理，旧容器由原Runner或管理员确认。

最终输出`restore-receipt.json`和`restore.compose.json`。后者包含新的应用存储凭证，限制权限且排除Git；不要打印或公开共享。**只有成功回执产生后才能切换**，提前产生的配置不是成功证明。失败保留明确的新目标供排查，不自动删除或启动部分恢复环境。

## 核验与人工切换

先在隔离目标检查原表/审计数量和文件SHA，并实际预览、下载、知识/私人检索；然后在新的维护窗口切换应用：

```sh
docker compose stop worker
docker compose stop sandbox-runner
docker compose stop api
docker compose -f compose.yaml -f backups/20261013/restore.compose.json up -d --wait api worker sandbox-runner
```

该操作保持Compose入口及网络，应用改用新库/Bucket/集合/执行存储卷；web仍连接同一API地址。后续迁移/备份时必须同步.env的数据库、应用Bucket/新身份/集合前缀，或显式给管理服务传入相同目标；不要仍对旧库执行维护。`docker compose config`会展开凭证，不要把完整输出贴到终端或日志。

M12只读审计入口`./scripts/verify-m12.ps1`或`sh scripts/verify-m12.sh`运行新增边界测试。真实恢复验收脚本`backend/tests/verify_m12_restored.py`仅允许m12_restore_前缀库，检索/预览在本地容器完成；云模型验证使用新注册且无历史/记忆/业务与知识权限的虚构账号，避免将恢复资料外发。

本地证据在`verification/M12/REPORT.md`。历史SQL确认、联网/RAG组合、附件转换、10任务排队、压缩和跨会话记忆复用前阶段已通过记录；不重新跑全格式/压力矩阵，不把有限验收声称为无限容量或任意宿主已验证。
