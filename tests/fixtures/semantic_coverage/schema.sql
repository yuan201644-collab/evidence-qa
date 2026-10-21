-- S2 要求覆盖闸门 —— 最小隔离样本 DDL
--
-- 上游规格：docs/architecture/限定条件保真与主动澄清修复实施方案_v0.1_20261015.md §7.1（:432-470）
--
-- ⚠ 本文件只允许在**独立库**执行。load.py 会拒绝指向 chinook / postgres(AW) /
--   cross_source_demo 的 DSN。原因见 README.md §1（T5 指纹事故）。
--
-- 表名与字段名刻意贴近 Chinook 命名（customer/invoice/employee），
-- 以便同一个「要求覆盖」判定器在样本与真实库上走同一条代码路径；
-- 但**行数与取值完全受控**，不依赖 Chinook 的任何内容。

BEGIN;

DROP TABLE IF EXISTS invoice CASCADE;
DROP TABLE IF EXISTS customer CASCADE;
DROP TABLE IF EXISTS employee CASCADE;
DROP TABLE IF EXISTS country_group CASCADE;
DROP TABLE IF EXISTS test_document CASCADE;

-- 客户：含明确国家、重复国家、以及一条国家为 NULL 的（母方案 :440-445）
CREATE TABLE customer (
    customer_id  integer      PRIMARY KEY,
    country      varchar(64),          -- 允许 NULL：未知国家
    city         varchar(64)
);

COMMENT ON COLUMN customer.country IS '客户所在国家。NULL 表示未知，不等于「非巴西」';
COMMENT ON COLUMN customer.city    IS '客户所在城市';

-- 发票：customer_id 为上表外键。total 单位为人民币元（母方案 :460）
CREATE TABLE invoice (
    invoice_id    integer       PRIMARY KEY,
    customer_id   integer       NOT NULL REFERENCES customer(customer_id),
    invoice_date  timestamptz   NOT NULL,
    total         numeric(12,2) NOT NULL
);

COMMENT ON COLUMN invoice.invoice_date IS '开票时间，UTC。时间锚点固定 2024-06-15';
COMMENT ON COLUMN invoice.total        IS '发票金额，单位：人民币元';

CREATE INDEX idx_invoice_date_customer ON invoice (invoice_date, customer_id);

-- 员工：只有编号与入职日期（母方案 :464 明确说明这是刻意的能力缺口）
CREATE TABLE employee (
    employee_id  integer     PRIMARY KEY,
    hire_date    date        NOT NULL
);

COMMENT ON COLUMN employee.hire_date IS '入职日期。本表无离职/有效期字段，历史在职人数不可由此表证明';

-- SC-20 独立分组样本：国家客户数 + 城市细分（母方案 :466）
CREATE TABLE country_group (
    country       varchar(64) NOT NULL,
    city          varchar(64) NOT NULL,
    customer_count integer    NOT NULL,
    PRIMARY KEY (country, city)
);

COMMENT ON TABLE  country_group IS 'SC-20 分组样本：客户数>5 的国家与城市细分';
COMMENT ON COLUMN country_group.customer_count IS '该国家该城市的客户数';

-- 文档样本：标题故意相同、合同号/设备/时限不同（母方案 :468）
CREATE TABLE test_document (
    doc_id            varchar(32) PRIMARY KEY,   -- TEST-DOC-A / TEST-DOC-B
    title             varchar(128) NOT NULL,     -- 两者**故意同名**
    contract_no       varchar(32)  NOT NULL,     -- CT-A / CT-B
    equipment         varchar(32)  NOT NULL,     -- EQ-A / EQ-B
    first_response_min integer     NOT NULL,     -- 30 / 60
    repair_hours       integer     NOT NULL,     -- 4 / 8
    body              text         NOT NULL      -- 真实文本，引用偏移由本文计算
);

COMMENT ON TABLE test_document IS 'SC-28/SC-29 文档样本：同名不同合同号，双方明确排除另一设备';

COMMIT;
