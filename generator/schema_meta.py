"""Deliberate variation axes for each schema. Used for tagging and the README matrix."""

SCHEMA_META = {
    "campus":          {"shape": ["many_to_many", "self_ref_m2m"], "naming": "snake_case",
                        "quoting": "unquoted", "flags": "int_0_1"},
    "org_chart":       {"shape": ["self_ref", "self_ref_m2m"], "naming": "PascalCase",
                        "quoting": "quoted", "flags": "enum"},
    "social_graph":    {"shape": ["self_ref_m2m", "self_ref", "many_to_many"], "naming": "camelCase",
                        "quoting": "quoted", "flags": "text_on_off"},
    "sales_star":      {"shape": ["star"], "naming": "snake_case",
                        "quoting": "unquoted", "flags": "int_0_1"},
    "sales_snowflake": {"shape": ["snowflake"], "naming": "PascalCase",
                        "quoting": "unquoted", "flags": "enum"},
    "library":         {"shape": ["many_to_many", "self_ref"], "naming": "camelCase",
                        "quoting": "unquoted", "flags": "text_on_off"},
    "web_analytics":   {"shape": ["star"], "naming": "camelCase",
                        "quoting": "quoted", "flags": "int_0_1"},
    "project_tracker": {"shape": ["many_to_many", "self_ref", "self_ref_m2m"], "naming": "snake_case",
                        "quoting": "unquoted", "flags": "enum"},
}
