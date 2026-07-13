import { describe, expect, it } from "vitest";
import { decodeQueryLink, encodeQueryLink } from "./queryLink";

describe("query link codec", () => {
  it("round-trips Chinese, newlines, quotes and symbols", () => {
    const original = {
      db: "shop",
      env: "prod",
      sql: "select '中文\"x\"'\nfrom orders where note like '%a&b=c%';",
    };
    const link = encodeQueryLink("http://localhost:9876/app/", original);
    const parsed = decodeQueryLink(new URL(link).search);
    expect(parsed).toEqual(original);
  });

  it("treats empty env as null", () => {
    const parsed = decodeQueryLink("?db=testpg&env=&sql=select%201");
    expect(parsed).toEqual({ db: "testpg", env: null, sql: "select 1" });
  });

  it("returns null when required params are missing", () => {
    expect(decodeQueryLink("?db=testpg")).toBeNull();
    expect(decodeQueryLink("?sql=select%201")).toBeNull();
  });
});
