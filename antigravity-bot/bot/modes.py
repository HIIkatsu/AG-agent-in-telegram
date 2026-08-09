"""Agent modes configuration."""

MODES = {
    "chat": {
        "name": "Chat", 
        "prompt": "General assistant. Help the user with their questions. Provide concise and accurate answers.", 
        "web": "auto"
    },
    "plan": {
        "name": "Plan", 
        "prompt": "Break tasks into step-by-step specs. Do not write full code. Focus on architecture, logic, and planning.", 
        "web": "off"
    },
    "code": {
        "name": "Code", 
        "prompt": "Write modular, clean code with strict typing. You are an expert software engineer.", 
        "web": "off"
    }
}

def get_mode_config(mode_key: str) -> dict:
    """Return the configuration for a given mode, fallback to 'code'."""
    return MODES.get(mode_key, MODES["code"])
