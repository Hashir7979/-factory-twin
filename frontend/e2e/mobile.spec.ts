import { expect, test } from "@playwright/test";

test("mobile layout has no horizontal scroll and keeps controls reachable", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#conn")).toHaveAttribute("data-state", "open");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await page.locator("#play").scrollIntoViewIfNeeded();
  await expect(page.locator("#play")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Experiments" })).toBeVisible();
});
