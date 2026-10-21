-- ============================================================
-- 政府采购监管业务库 schema（askdata_gov）
-- 数据来源：中国政府采购网（www.ccgp.gov.cn）真实公告
-- 生成方式：从公告正文抽取，无合成数据
-- 版本：v1.0  日期：2026-10-17
--
-- 表关系（v1.1，2026-10-17 补规范维度）：
--   regions  ← projects.region_code（外键，标准维度表）
--   projects ← announcements / packages / awards / documents
--   packages ← items / awards
--   suppliers ← awards
--   documents 同时挂 project_id 与 source_url，是跨源桥
--
-- v1.1 改动理由：v1.0 里 projects.region_name 只是文本列、没有外键，
-- 「采购项目分布在多少个省级行政区」这类两表题选对了表却建不出连接边，
-- 链路直接报"无法建立唯一且有元数据依据的表关联路径"。
-- 补成外键后 region 成为标准维度表，任何按地区聚合的题都只有一条路径。
-- 注：v1.0 的说明里写过「buyers / agencies 也挂 region_name」，与建表语句不符
-- （它们本来就没有这一列），此处一并更正。
-- ============================================================

DROP TABLE IF EXISTS regulation_rules CASCADE;
DROP TABLE IF EXISTS documents CASCADE;
DROP TABLE IF EXISTS awards CASCADE;
DROP TABLE IF EXISTS items CASCADE;
DROP TABLE IF EXISTS packages CASCADE;
DROP TABLE IF EXISTS announcements CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP TABLE IF EXISTS suppliers CASCADE;
DROP TABLE IF EXISTS agencies CASCADE;
DROP TABLE IF EXISTS buyers CASCADE;
DROP TABLE IF EXISTS regions CASCADE;

-- 1. 行政区划
CREATE TABLE regions (
    region_code   VARCHAR(12)  PRIMARY KEY,
    region_name   VARCHAR(64)  NOT NULL,
    level         SMALLINT     NOT NULL DEFAULT 1
);
COMMENT ON TABLE regions IS '省级行政区划，取自公告「行政区域」字段的首级';

-- 2. 采购单位
CREATE TABLE buyers (
    buyer_id            BIGSERIAL    PRIMARY KEY,
    buyer_name          VARCHAR(300) NOT NULL UNIQUE,
    announcement_count  INTEGER      NOT NULL DEFAULT 0
);
COMMENT ON TABLE buyers IS '采购单位（采购人），由公告「采购单位」字段去重';

-- 3. 代理机构
CREATE TABLE agencies (
    agency_id           BIGSERIAL    PRIMARY KEY,
    agency_name         VARCHAR(300) NOT NULL UNIQUE,
    announcement_count  INTEGER      NOT NULL DEFAULT 0
);
COMMENT ON TABLE agencies IS '政府采购代理机构，由公告「代理机构名称」字段去重';

-- 4. 供应商
CREATE TABLE suppliers (
    supplier_id         BIGSERIAL    PRIMARY KEY,
    supplier_name       VARCHAR(300) NOT NULL UNIQUE,
    award_count         INTEGER      NOT NULL DEFAULT 0
);
COMMENT ON TABLE suppliers IS '供应商，由中标公告「供应商名称」与正文公司名去重';

-- 5. 采购项目（主表）
CREATE TABLE projects (
    project_id          VARCHAR(120) PRIMARY KEY,
    project_name        VARCHAR(500) NOT NULL,
    buyer_id            BIGINT       NOT NULL REFERENCES buyers(buyer_id),
    -- agency_id 允许为空：实测有 2 个项目的公告正文没写代理机构
    -- （如「新华通讯社技术系统升级改造二期项目新闻大厦…」）。这是源数据
    -- 缺失，不是抽取失败，用空值如实表达；若强行 NOT NULL 只能编一个代理机构。
    agency_id           BIGINT       REFERENCES agencies(agency_id),
    region_code         VARCHAR(12)  REFERENCES regions(region_code),
    procurement_way     VARCHAR(24),
    budget_yuan         NUMERIC(16,2),
    first_announce_date DATE,
    last_announce_date  DATE,
    announcement_count  INTEGER      NOT NULL DEFAULT 0,
    -- 预算级次：central 中央预算 / local 地方预算 / unknown 未知。
    -- v1.1 前由 infra/scripts/add_budget_level.py 事后 ALTER 补上，
    -- 重建库时该列会丢、题集随之报 UndefinedColumn，故并入主 DDL。
    budget_level        VARCHAR(12)  NOT NULL DEFAULT 'unknown'
);
COMMENT ON TABLE projects IS '采购项目主表，project_id 为公告里的项目编号（业务主键）';
CREATE INDEX idx_projects_buyer  ON projects(buyer_id);
CREATE INDEX idx_projects_agency ON projects(agency_id);
CREATE INDEX idx_projects_region ON projects(region_code);
CREATE INDEX idx_projects_way    ON projects(procurement_way);
CREATE INDEX idx_projects_date   ON projects(first_announce_date);
CREATE INDEX idx_projects_budget ON projects(budget_yuan);

-- 6. 公告
CREATE TABLE announcements (
    announcement_id     BIGSERIAL    PRIMARY KEY,
    project_id          VARCHAR(120) NOT NULL REFERENCES projects(project_id),
    title               VARCHAR(600) NOT NULL,
    kind                VARCHAR(24)  NOT NULL,
    publish_date        DATE,
    source_url          VARCHAR(600) NOT NULL UNIQUE,
    body_chars          INTEGER,
    has_items           BOOLEAN      NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE announcements IS '公告事实表，一个项目可有多条公告（招标/更正/中标/终止）';
CREATE INDEX idx_ann_project ON announcements(project_id);
CREATE INDEX idx_ann_date    ON announcements(publish_date);
CREATE INDEX idx_ann_kind    ON announcements(kind);

-- 7. 包组
CREATE TABLE packages (
    package_id          BIGSERIAL    PRIMARY KEY,
    project_id          VARCHAR(120) NOT NULL REFERENCES projects(project_id),
    package_no          VARCHAR(16)  NOT NULL,
    budget_yuan         NUMERIC(16,2),
    award_yuan          NUMERIC(16,2)
);
COMMENT ON TABLE packages IS '包组，一个项目可拆成多个包分别中标';
CREATE UNIQUE INDEX uq_package ON packages(project_id, package_no);

-- 8. 采购标的
CREATE TABLE items (
    item_id             BIGSERIAL    PRIMARY KEY,
    announcement_id     BIGINT       NOT NULL REFERENCES announcements(announcement_id),
    package_id          BIGINT       REFERENCES packages(package_id),
    item_seq            SMALLINT,
    item_name           VARCHAR(500),
    brand               VARCHAR(300),
    model               VARCHAR(300),
    category_code       VARCHAR(32),
    category_name       VARCHAR(300),
    quantity            NUMERIC(14,3),
    quantity_unit       VARCHAR(16),
    unit_price_yuan     NUMERIC(16,2),
    total_price_yuan    NUMERIC(16,2)
);
COMMENT ON TABLE items IS '采购标的明细，来自公告「四、主要标的信息」区块';
CREATE INDEX idx_items_ann ON items(announcement_id);
CREATE INDEX idx_items_pkg ON items(package_id);

-- 9. 中标结果
CREATE TABLE awards (
    award_id            BIGSERIAL    PRIMARY KEY,
    project_id          VARCHAR(120) NOT NULL REFERENCES projects(project_id),
    package_id          BIGINT       NOT NULL REFERENCES packages(package_id),
    supplier_id         BIGINT       NOT NULL REFERENCES suppliers(supplier_id),
    award_yuan          NUMERIC(16,2),
    review_score        NUMERIC(6,2),
    award_date          DATE,
    announcement_id     BIGINT       NOT NULL REFERENCES announcements(announcement_id)
);
COMMENT ON TABLE awards IS '中标事实：哪个供应商、中哪个包、多少钱';
CREATE INDEX idx_awards_supplier ON awards(supplier_id);
CREATE INDEX idx_awards_project  ON awards(project_id);
CREATE INDEX idx_awards_amount   ON awards(award_yuan);

-- 10. 文档桥（跨源对齐）
CREATE TABLE documents (
    document_id         BIGSERIAL    PRIMARY KEY,
    doc_code            VARCHAR(120) NOT NULL UNIQUE,
    project_id          VARCHAR(120) NOT NULL REFERENCES projects(project_id),
    announcement_id     BIGINT       NOT NULL REFERENCES announcements(announcement_id),
    doc_type            VARCHAR(24)  NOT NULL,
    title               VARCHAR(600) NOT NULL,
    source_url          VARCHAR(600),
    local_path          VARCHAR(600),
    page_count          INTEGER,
    char_count          INTEGER,
    checksum            VARCHAR(64)
);
COMMENT ON TABLE documents IS '跨源桥：把知识库文档与数据库记录对齐到同一个 project_id';
CREATE INDEX idx_docs_project ON documents(project_id);
CREATE INDEX idx_docs_type    ON documents(doc_type);

-- 11. 规则表（人工整理，用于合规判断）
CREATE TABLE regulation_rules (
    rule_id             VARCHAR(24)  PRIMARY KEY,
    regulation          VARCHAR(200) NOT NULL,
    article             VARCHAR(120) NOT NULL,
    condition_field     VARCHAR(120),
    condition_op        VARCHAR(12),
    condition_value     VARCHAR(200),
    conclusion          VARCHAR(500) NOT NULL,
    source_snippet      TEXT         NOT NULL,
    source_document     VARCHAR(300),
    source_url          VARCHAR(600),
    note                VARCHAR(300)
);
COMMENT ON TABLE regulation_rules IS '政府采购核心规则，人工从法规原文整理，source_snippet 为原文片段用于溯源';
