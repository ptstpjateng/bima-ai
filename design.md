# Design System Specification: The Ethereal Slate

## 1. Overview & Creative North Star

**Creative North Star: "The Digital Observatory"**

The objective of this design system is to move away from the "flat box" aesthetic of traditional admin panels and toward a high-fidelity, immersive environment. We are building a "Digital Observatory"—a place of clarity, depth, and precision. By leveraging deep slate tones and frosted glass textures, we create a UI that feels less like a webpage and more like a high-end physical console.

The "template" look is broken through **intentional depth**. We do not use lines to separate ideas; we use light and opacity. The interface should feel as though it is composed of suspended layers of polished glass floating over a vast, dark void.

## 2. Colors & Surface Philosophy

Our palette is rooted in the deep spectrum of slate and indigo, optimized for high-end dark mode environments.

### The "No-Line" Rule

Standard 1px solid borders are strictly prohibited for sectioning or layout containment. Boundaries must be defined through **Background Color Shifts** or **Tonal Transitions**.

* *Example:* To separate a sidebar from the main content, do not draw a line. Instead, set the Sidebar to `surface_container_low` and the Main Content area to `surface`.

### Surface Hierarchy & Nesting

Depth is achieved by "stacking" surface tiers. Think of these as sheets of glass with varying densities:

* **Base Layer:** `surface` (#0b1326) — The infinite background.
* **Section Layer:** `surface_container_low` (#131b2e) — Large layout blocks.
* **Card/Component Layer:** `surface_container_highest` (#2d3449) at 60% opacity with `backdrop-blur` (20px-40px).

### The "Glass & Gradient" Rule

For primary actions and high-level summaries, use a **Signature Texture**:

* **Primary Gradient:** A subtle linear flow from `primary_container` (#4f46e5) to `primary` (#c3c0ff) at a 135-degree angle. This provides a "soul" to the UI that flat hex codes cannot achieve.

## 3. Typography: Manrope

Manrope is a geometric sans-serif that strikes a balance between technical precision and modern warmth.

* **Display & Headlines:** Use `display-md` or `headline-lg` with `letter-spacing: -0.02em`. This "tight" tracking creates an authoritative, editorial feel for data totals and page titles.
* **Body Copy:** Utilize `body-md` for standard data. Ensure `on_surface_variant` is used for secondary labels to maintain a clear visual hierarchy against the primary white text.
* **Data Labels:** `label-md` should always be in `uppercase` with a `letter-spacing: 0.05em` to differentiate metadata from actionable content.

## 4. Elevation & Depth

### The Layering Principle

Avoid shadows on nested elements. Instead, use the **Tonal Stack**:

1. **Level 0 (Backdrop):** `surface`
2. **Level 1 (Panels):** `surface_container`
3. **Level 2 (Cards):** `surface_container_high` + Glassmorphism.

### Ambient Shadows

For "floating" elements like Popovers or Modals, use an **Ambient Glow**:

* **Shadow:** 0px 20px 40px rgba(0, 0, 0, 0.4).
* **Tint:** Add a 1px inner-stroke (top-down) using `outline_variant` at 15% opacity to simulate light hitting the top edge of the glass.

### The "Ghost Border" Fallback

If a visual separator is required for accessibility in complex tables, use a "Ghost Border": `outline_variant` (#464555) at **10% opacity**. It should be felt, not seen.

## 5. Components

### Buttons

* **Primary:** Gradient fill (`primary_container` to `primary`). `border-radius: ROUND_EIGHT`. No border.
* **Secondary:** Glass-style. `surface_container_highest` at 40% opacity, `backdrop-blur: 10px`, with a 1px ghost border.
* **Tertiary:** Ghost style. No background, `on_surface` text, `primary` color on hover.

### Data Tables (The Filament Hook)

* **Header:** `surface_container_low` background. No vertical lines.
* **Rows:** 1px `outline_variant` at 5% opacity on the bottom edge only.
* **Hover State:** Transition background to `surface_container_high` with a subtle `primary` left-border accent (2px).

### Input Fields

* **Base:** `surface_container_lowest` (#060e20).
* **Border:** `outline_variant` at 20% opacity.
* **Focus:** Border becomes `primary` (#c3c0ff) with a 4px `primary_container` outer glow at 20% opacity.

### Chips

* **Selection:** Indigo tint (`primary_container`) with 20% opacity background and 100% opacity text. This creates a "light-box" effect.

## 6. Do's and Don'ts

### Do:

* **Use Negative Space:** Use the `spacing-8` (2rem) and `spacing-12` (3rem) tokens generously between glass cards to let the background "breathe."
* **Layer Glass:** Place a `primary` colored glow (low opacity circle) *behind* a glass card to create a modern "blob" aesthetic that highlights a specific metric.
* **Typography over Icons:** Let the Manrope typeface do the heavy lifting. Only use icons when they provide immediate functional recognition.

### Don't:

* **Don't use 100% Black:** Never use #000000. Use `surface` (#0b1326) to maintain the slate-blue depth.
* **Don't use Dividers:** Avoid `<hr>` or visual lines to separate list items. Use vertical padding (`spacing-4`) and background shifts instead.
* **Don't Over-Blur:** Keep `backdrop-blur` between 12px and 25px. Anything higher loses the "glass" quality and starts looking like a flat solid color.
