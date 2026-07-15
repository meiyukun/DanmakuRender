# 流水线恢复开关

本次新增 `recover_pipeline` 配置，用于控制程序重启时是否加载录制任务的未完成
流水线状态及其待投递消息。

- 在 `global.yml` 的 `dmr_engine_args.recover_pipeline` 设置全局默认值。
- 在单个 `DMR-*.yml` 的 `common_event_args.recover_pipeline` 设置任务级覆盖。
- 设为 `false` 时，启动会清除该任务的恢复账本和 outbox，不会删除媒体文件。
