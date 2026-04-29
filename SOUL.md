# Media Request MCP Agent Guidance

- Use `search_media` for media search and for questions like "do we have X?"
- Use `request_movie` for movie requests.
- Use `request_series` for series requests.
- For series, always pass explicit season numbers in `seasons`; whole-show
  requests still need every wanted season listed.
- Use `browse_library` for recommendations, then rank and explain candidates in
  the agent response.
- Treat library availability as factual. Movies require a movie file; series
  require episode files.
- Do not invent ETAs. Only report progress, time left, or ETA from active queue
  items.
