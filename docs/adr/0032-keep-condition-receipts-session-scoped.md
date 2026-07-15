# 条件回执只存在于函数会话

条件回执保存在函数级 workflow session state，只跨当前会话内的重新分析，不写入 BNDB。重新
打开 BNDB 时，deinbr 从当前 LLIL 重新取得条件事实，并以 Binary Ninja 已保存的用户分支目标
作一致性校验；translator 仍不执行目标解码。持久化 IL 定位信息需要额外的版本、失效和迁移
协议，而且可能把旧 Binary Ninja IL 形态误当成当前证明，因此不纳入核心状态。

会话内仅在 provider 绑定改变、branch-target 或 branch-translation pass 关闭、所属分支目标
回执改变/消失，或同源新鲜事实明确替换 condition 或 true/false 目标时使回执失效。translator
重绑、形态、copy-transform 或安装失败不会删除回执；成功安装 IF 也保留回执，以便当前 MLIL
重新判定 `ALREADY_SATISFIED` 并继续控制 cleanup/deflatten 门。
