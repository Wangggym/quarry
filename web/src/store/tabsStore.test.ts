import { describe, expect, it } from "vitest";
import { parseMainTable, tabTitle, type Tab } from "./tabsStore";

function tab(patch: Partial<Tab>): Tab {
  return { id: "t1", title: null, sql: "", db: null, env: null, ...patch };
}

describe("parseMainTable", () => {
  it("finds the table in a single-table SELECT", () => {
    expect(parseMainTable("select id, name from mind_trace where id = 1")).toBe("mind_trace");
  });

  it("finds the table in UPDATE/INSERT/DELETE", () => {
    expect(parseMainTable("update mind_attribute set v = 1 where id = 1")).toBe("mind_attribute");
    expect(parseMainTable("insert into mind_attribute (a, b) values (1, 2)")).toBe("mind_attribute");
    expect(parseMainTable("delete from mind_trace where id = 1")).toBe("mind_trace");
  });

  it("returns null for a multi-table JOIN", () => {
    expect(parseMainTable("select a.id from mind_trace a join mind_attribute b on a.id = b.id")).toBeNull();
  });

  it("returns null for a non-DML statement", () => {
    expect(parseMainTable("explain analyze select 1")).toBeNull();
  });

  it("finds the table when it's quoted (mixed-case/reserved names)", () => {
    // Postgres double-quote form, as produced by ResultWorkbench's quoteIdent()
    expect(parseMainTable('select * from "QyCamelZz" limit 5')).toBe("QyCamelZz");
    // schema-qualified, quoted
    expect(parseMainTable('select * from public."QyCamelZz" limit 5')).toBe("QyCamelZz");
    // MySQL backtick form
    expect(parseMainTable("select * from `QyCamelZz` limit 5")).toBe("QyCamelZz");
    // escaped quote inside the identifier
    expect(parseMainTable('select * from "Weird""Name" limit 5')).toBe('Weird"Name');
  });
});

describe("tabTitle", () => {
  it("prefers a user-set title over anything else", () => {
    expect(tabTitle(tab({ title: "My tab", sql: "select * from mind_trace", db: "shop", env: "prod" }))).toBe(
      "My tab",
    );
  });

  it("derives the title from the SQL's main table, distinguishing same-connection tabs", () => {
    const t1 = tab({ sql: "select * from mind_trace", db: "shop", env: "prod" });
    const t2 = tab({ sql: "select * from mind_attribute", db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe("mind_trace");
    expect(tabTitle(t2)).toBe("mind_attribute");
    expect(tabTitle(t1)).not.toBe(tabTitle(t2));
  });

  it("distinguishes tabs querying quoted mixed-case tables", () => {
    const t1 = tab({ sql: 'select * from "QyCamelZz" limit 5', db: "shop", env: "prod" });
    const t2 = tab({ sql: 'select * from "OtherCamel" limit 5', db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe("QyCamelZz");
    expect(tabTitle(t2)).toBe("OtherCamel");
  });

  it("allows same-table queries to share a title", () => {
    const t1 = tab({ sql: "select id from mind_trace", db: "shop", env: "prod" });
    const t2 = tab({ sql: "select name from mind_trace", db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe(tabTitle(t2));
  });

  it("falls back to the first SQL words when no single table can be parsed", () => {
    expect(tabTitle(tab({ sql: "explain analyze select 1", db: "shop", env: "prod" }))).toBe("explain analyze");
  });

  it("falls back to db@env when there is no SQL", () => {
    expect(tabTitle(tab({ sql: "  ", db: "shop", env: "prod" }))).toBe("shop@prod");
  });

  it("falls back to the new-query placeholder with neither SQL nor a connection", () => {
    expect(tabTitle(tab({}))).toBe("new query");
  });
});
