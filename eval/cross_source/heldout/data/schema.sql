CREATE SCHEMA IF NOT EXISTS cross_source_demo;
CREATE TABLE IF NOT EXISTS cross_source_demo.equipment (
    equipment_id TEXT PRIMARY KEY, equipment_name TEXT NOT NULL,
    customer_id TEXT NOT NULL, site_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cross_source_demo.contracts (
    contract_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
    effective_from DATE NOT NULL, effective_to DATE NOT NULL,
    status TEXT NOT NULL, timezone TEXT NOT NULL,
    CHECK (effective_to >= effective_from)
);
CREATE TABLE IF NOT EXISTS cross_source_demo.contract_equipment (
    contract_id TEXT NOT NULL REFERENCES cross_source_demo.contracts(contract_id),
    equipment_id TEXT NOT NULL REFERENCES cross_source_demo.equipment(equipment_id),
    valid_from DATE NOT NULL, valid_to DATE NOT NULL, mapping_basis TEXT NOT NULL,
    PRIMARY KEY (contract_id, equipment_id), CHECK (valid_to >= valid_from)
);
CREATE TABLE IF NOT EXISTS cross_source_demo.work_orders (
    work_order_id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL REFERENCES cross_source_demo.equipment(equipment_id),
    opened_at TIMESTAMPTZ NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cross_source_demo.work_order_events (
    work_order_id TEXT NOT NULL REFERENCES cross_source_demo.work_orders(work_order_id),
    event_type TEXT NOT NULL, event_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (work_order_id, event_type)
);
