import { type Page, expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

function trackErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  return errors;
}

test("live simulation streams events and updates KPIs", async ({ page }) => {
  const errors = trackErrors(page);
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  await expect(page.locator("#overlay")).toBeHidden();
  await expect(page.locator(".machine-label", { hasText: "Laser Welding" })).toBeVisible();

  await page.locator("#speed").fill("900");
  await page.locator("#play").click();
  await expect(page.locator("#play")).toHaveText("Pause");
  await expect(page.locator("#sim-clock")).not.toHaveText("00:00:00");
  await expect(page.locator(".kpi-card").first().locator(".kpi-value")).not.toHaveText("–", { timeout: 30_000 });
  await expect(page.locator("#stations-body tr")).toHaveCount(6);
  expect(errors).toEqual([]);
});

test("clicking a machine opens the inspector and failure injection takes effect", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  const label = page.locator(".machine-label", { hasText: "Laser Welding" });
  const box = (await label.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height + 40);
  const inspector = page.locator("#inspector");
  await expect(inspector).toBeVisible();
  await expect(inspector.locator("h3")).toHaveText("Laser Welding");
  await inspector.getByRole("button", { name: "Break down now" }).click();
  await expect(inspector.locator(".state-chip")).toHaveText("Down");
  await expect(label).toHaveAttribute("data-state", "down");
  await page.keyboard.press("Escape");
  await expect(inspector).toBeHidden();
});

test("what-if form validates input and restarts with new machines", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  await page.getByRole("tab", { name: "What-if" }).click();
  const servers = page.locator('input[name="station.weld.servers"]');
  await servers.fill("0");
  await expect(servers).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByRole("button", { name: "Apply & restart" })).toBeDisabled();
  await servers.fill("2");
  await page.getByRole("button", { name: "Apply & restart" }).click();
  await expect(page.locator(".machine-label", { hasText: "Laser Welding 2" })).toBeVisible();
});

test("replication study compares baseline and what-if with confidence intervals", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  await page.getByRole("tab", { name: "What-if" }).click();
  await page.locator('input[name="station.weld.servers"]').fill("2");
  await page.getByRole("tab", { name: "Experiments" }).click();
  await page.locator("#exp-reps").fill("4");
  await page.locator("#experiments").getByRole("button", { name: "Run experiment" }).click();
  const results = page.locator("#experiments .results");
  await expect(results.locator(".result-table")).toBeVisible({ timeout: 60_000 });
  await expect(results).toContainText("Throughput");
  await expect(results).toContainText("±");
  await expect(results.getByRole("link", { name: /CSV/ })).toHaveAttribute("href", /runs\.csv$/);
});

test("scenario switch rebuilds the scene and updates the URL", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  await page.locator("#scenario-select").selectOption("fulfillment_center");
  await expect(page).toHaveURL(/scenario=fulfillment_center/);
  await expect(page.locator(".machine-label", { hasText: "Packing" }).first()).toBeVisible();
});

test("earthmoving scenario shows trucks, the fleet constraint and a fleet-size control", async ({ page }) => {
  const errors = trackErrors(page);
  await page.goto("/?scenario=earthmoving");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  await expect(page.locator(".machine-label", { hasText: "Excavator" })).toBeVisible();
  await expect(page.locator(".buffer-label", { hasText: "Haul Road" })).toBeVisible();
  await expect(page.locator(".kpi-card").nth(3).locator(".kpi-label")).toHaveText("Production");

  await page.locator("#speed").fill("1000");
  await page.locator("#play").click();
  await expect(page.locator("#bottleneck")).toContainText("Truck fleet", { timeout: 30_000 });

  await page.getByRole("tab", { name: "What-if" }).click();
  const trucks = page.locator('input[name="source.max_arrivals"]');
  await expect(trucks).toHaveValue("4");
  await trucks.fill("21");
  await expect(trucks).toHaveAttribute("aria-invalid", "true"); // roads hold at most 20 trucks
  await trucks.fill("7");
  await page.getByRole("button", { name: "Apply & restart" }).click();
  await page.getByRole("tab", { name: "Optimize" }).click();
  await expect(page.locator("#optimizer .opt-vars")).toContainText("Trucks");
  await expect(page.locator("#optimizer")).toContainText("Truck cost");
  expect(errors).toEqual([]);
});
