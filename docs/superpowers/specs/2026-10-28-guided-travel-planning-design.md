# Guided Travel Planning Design

## Intent

Replace the new-trip chat entry with a form-led planning flow. Keep the existing LangGraph chat kernel for later edits and use a LangGraph coordinator with scoped specialist nodes for the guided flow.

## Flow

1. The traveler enters destination, departure city or home anchor, departure date and trip length, party size, CNY budget, daily food and local transit allowances, interests, pace and constraints. The return date is derived from the trip length. The API also accepts an explicit return date for existing clients. Invalid input is rejected before saving.
2. Transport and hotel specialists produce comparable candidates. Flight, train and hotel prices are reproducible simulations, clearly labeled as such. The traveler selects outbound and return transport and one hotel. Search never books or charges.
3. A guide specialist recognizes text from an uploaded image and extracts candidate places and notes for correction. An activity specialist adds recommended places; without a map key it offers clearly labeled themes that require a specific place before confirmation. The traveler marks places as must-go, optional or excluded.
4. A route specialist drafts a day-by-day itinerary from the selected places and travel times. Each day has explicit start and end anchors. Arrival and departure days account for selected transport; intermediate days start and end at the hotel or specified home.
5. An independent audit checks budget, schedule, route continuity, and anchor rules. Chat-added budget items are included and flagged for possible overlap. The traveler can edit choices and regenerate. A confirmed draft yields a printable itinerary sheet.
6. Selected transport, hotel and ticket items can be marked purchased with an actual total paid price. Actual paid prices replace corresponding estimates in budget accounting. Replanning preserves purchases and warns when it affects one.

## Architecture and constraints

- The main LangGraph coordinator owns transitions and persistence. Specialist nodes return structured proposals and never independently overwrite confirmed selections.
- Reuse the existing MySQL travel plan, HTTP API, React frontend, map tools and LangGraph chat. Add a single JSON planning field to the travel plan for guided-flow state.
- Preserve existing plans and chat behavior. No payment or supplier booking integration is in scope.
- An external OCR runtime may be required; missing OCR must produce a visible error, never fabricated text.
- Unknown route travel times and costs remain explicitly estimates. A high-severity audit issue blocks confirmation.

## Approaches

- Chosen: form-led wizard plus LangGraph coordinator and specialist nodes. This gives clear traveler decision gates and durable state while sharing the current graph-based execution approach.
- Considered: one free-form chat agent. It cannot reliably enforce candidate selection or purchase tracking.
- Considered: independent specialist agents writing directly to the plan. Concurrent writes could replace the traveler's choices.
