FROM rust:1.83 as builder

WORKDIR /app
COPY . .

RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制编译产物
COPY --from=builder /app/target/release/btc_futures_sniper /app/

# 创建数据目录
RUN mkdir -p /app/data

# 设置环境变量默认值
ENV DRY_RUN=true
ENV INITIAL_BALANCE=1000.0
ENV TELEGRAM_ENABLED=true

CMD ["./btc_futures_sniper"]
