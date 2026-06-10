FROM rust:latest as builder

WORKDIR /app

# 先复制依赖文件，利用Docker缓存
COPY Cargo.toml ./
RUN mkdir src && \
    echo "fn main() {}" > src/main.rs && \
    cargo build --release && \
    rm -rf src

# 再复制源代码编译
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim

WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制编译产物
COPY --from=builder /app/target/release/btc-futures-sniper /app/

# 创建数据目录
RUN mkdir -p /app/data

# 设置环境变量默认值
ENV DRY_RUN=true
ENV INITIAL_BALANCE=1000.0
ENV TELEGRAM_ENABLED=true

CMD ["./btc-futures-sniper"]