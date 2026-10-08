-- ============================================================
-- 广东电力市场价格数据采集 —— 数据库表结构
-- 适用 MySQL 8.0+ / MariaDB 10.5+
-- 执行：mysql -u root -p < schema.sql
-- ============================================================

CREATE DATABASE IF NOT EXISTS gd_power
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_general_ci;

USE gd_power;

-- ------------------------------------------------------------
-- 1. 每日均价（对应披露条目 6.58 现货市场出清均价）
--    粒度：日 × 价格类型
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_daily_price (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  trade_date    DATE            NOT NULL COMMENT '交易日期',
  price_type    VARCHAR(20)     NOT NULL COMMENT '日前/实时',
  avg_price     DECIMAL(10,4)   NULL     COMMENT '全天均价 元/MWh',
  max_price     DECIMAL(10,4)   NULL     COMMENT '全天最高 元/MWh',
  min_price     DECIMAL(10,4)   NULL     COMMENT '全天最低 元/MWh',
  peak_avg      DECIMAL(10,4)   NULL     COMMENT '峰段均价 元/MWh',
  flat_avg      DECIMAL(10,4)   NULL     COMMENT '平段均价 元/MWh',
  valley_avg    DECIMAL(10,4)   NULL     COMMENT '谷段均价 元/MWh',
  source        VARCHAR(50)     NULL     COMMENT '数据来源站点',
  collected_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_date_type (trade_date, price_type),
  KEY idx_date (trade_date)
) ENGINE=InnoDB COMMENT='每日均价汇总';

-- ------------------------------------------------------------
-- 2. 96点分时价格（核心表）
--    粒度：日 × 价格类型 × 15分钟时段
--    节点类型区分：统一出清价 / 分区节点价 / 地市节点价
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_spot_96 (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  trade_date    DATE            NOT NULL COMMENT '交易日期',
  price_type    VARCHAR(20)     NOT NULL COMMENT '日前/实时',
  node_type     VARCHAR(20)     NOT NULL DEFAULT 'system'
                                COMMENT 'system=统一出清, region=分区, city=地市',
  node_code     VARCHAR(50)     NOT NULL DEFAULT 'ALL' COMMENT '节点编码，如 GZ/SZ',
  node_name     VARCHAR(50)     NULL     COMMENT '节点名称，如 广州/深圳',
  seq           TINYINT UNSIGNED NOT NULL COMMENT '时段序号 1-96',
  time_point    TIME            NOT NULL COMMENT '时段起点，如 00:00, 00:15',
  price         DECIMAL(10,4)   NULL     COMMENT '该时段电价 元/MWh',
  collected_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_date_type_node_seq (trade_date, price_type, node_type, node_code, seq),
  KEY idx_date (trade_date),
  KEY idx_node (node_type, node_code, trade_date)
) ENGINE=InnoDB COMMENT='96点分时电价';

-- ------------------------------------------------------------
-- 3. 地市峰谷价（对应门户"现货价格地区分布"栏目）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_city_peakvalley (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  trade_date    DATE            NOT NULL,
  price_type    VARCHAR(20)     NOT NULL COMMENT '日前/实时',
  city_name     VARCHAR(50)     NOT NULL COMMENT '地市名称',
  city_code     VARCHAR(20)     NULL     COMMENT '地市编码',
  peak_time     TIME            NULL     COMMENT '高峰时刻',
  peak_price    DECIMAL(10,4)   NULL     COMMENT '高峰时刻电价 元/MWh',
  valley_time   TIME            NULL     COMMENT '低谷时刻',
  valley_price  DECIMAL(10,4)   NULL     COMMENT '低谷时刻电价 元/MWh',
  spread        DECIMAL(10,4)   NULL     COMMENT '峰谷价差（计算列冗余存储）',
  collected_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_date_type_city (trade_date, price_type, city_name),
  KEY idx_date (trade_date)
) ENGINE=InnoDB COMMENT='地市峰谷电价';

-- ------------------------------------------------------------
-- 4. 中长期市场行情（月度，对应 6.20）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_midlong (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  period        VARCHAR(20)     NOT NULL COMMENT '如 2026-09 / 2026',
  period_type   VARCHAR(10)     NOT NULL COMMENT 'month/year',
  category      VARCHAR(50)     NULL     COMMENT '年度/月度/多日/绿电',
  volume        DECIMAL(18,4)   NULL     COMMENT '成交电量 万kWh',
  avg_price     DECIMAL(10,4)   NULL     COMMENT '成交均价 元/MWh',
  collected_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_period_cat (period, category),
  KEY idx_period (period)
) ENGINE=InnoDB COMMENT='中长期市场行情';

-- ------------------------------------------------------------
-- 5. 采集日志（用于排障与断点续采）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collect_log (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  run_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  task_name     VARCHAR(50)     NOT NULL COMMENT '任务名，如 daily_price',
  trade_date    DATE            NULL,
  status        VARCHAR(20)     NOT NULL COMMENT 'success/fail/partial',
  rows_affected INT             DEFAULT 0,
  message       TEXT            NULL,
  KEY idx_run (run_at),
  KEY idx_task (task_name, trade_date)
) ENGINE=InnoDB COMMENT='采集日志';

-- ------------------------------------------------------------
-- 6. 原始响应存档（防解析错误导致数据丢失，便于回溯重解析）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_snapshot (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  run_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  task_name     VARCHAR(50)     NOT NULL,
  trade_date    DATE            NULL,
  url           VARCHAR(500)    NULL,
  payload       MEDIUMTEXT      NULL     COMMENT '原始JSON/HTML',
  KEY idx_task_date (task_name, trade_date)
) ENGINE=InnoDB COMMENT='原始响应存档';

-- ------------------------------------------------------------
-- 7. 气象数据（Open-Meteo，可另存 SQLite，此处一并纳管）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_weather (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ts              DATETIME        NOT NULL COMMENT '小时整点',
  site            VARCHAR(50)     NOT NULL COMMENT '场址名，如 yangjiang_offshore',
  latitude        DECIMAL(9,6)    NULL,
  longitude       DECIMAL(9,6)    NULL,
  wind_speed_100m DECIMAL(8,2)    NULL COMMENT '100米风速 km/h',
  wind_dir_100m   SMALLINT        NULL COMMENT '100米风向 度',
  shortwave_rad   DECIMAL(8,2)    NULL COMMENT '短波辐照 W/m2',
  temperature_2m  DECIMAL(6,2)    NULL COMMENT '2米气温 ℃',
  is_forecast     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '1=预报, 0=历史实测',
  collected_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_ts_site_src (ts, site, is_forecast),
  KEY idx_site_ts (site, ts)
) ENGINE=InnoDB COMMENT='气象数据';

-- ------------------------------------------------------------
-- 视图：峰谷价差日序列（方便直接喂给模型）
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_peak_valley_spread AS
SELECT
  trade_date,
  price_type,
  AVG(peak_price)                       AS avg_peak,
  AVG(valley_price)                     AS avg_valley,
  AVG(peak_price) - AVG(valley_price)   AS avg_spread,
  COUNT(*)                              AS city_cnt
FROM fact_city_peakvalley
GROUP BY trade_date, price_type;
