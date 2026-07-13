# DispatchThis 仅面向 ARM64 ELF 样本

DispatchThis 将明确面向用户反复遇到的 ARM64 ELF 样本族。原插件的 pipeline 仍有价值，但若
保留旧有的非 ARM64 样本支持，会让无关的解码、调用和状态机假设干扰更窄的日常分析流程，
因此接受这一取舍。
