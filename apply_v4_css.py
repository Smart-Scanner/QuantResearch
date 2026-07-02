import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Update radius
content = re.sub(r'--radius:\s*12px;', '--radius: 6px;', content)

# Update Dark Mode Colors
dark_mode_replacement = """        [data-theme="dark"] {
            --bg-primary: #0B1220;
            --bg-secondary: #111827;
            --bg-tertiary: #0B1220;
            --bg-hover: #1F2937;
            --bg-card: #111827;
            --sidebar-bg: #111827;
            --sidebar-hover: #1F2937;
            --sidebar-active: #1F2937;
            --header-bg: #111827;
            --text: #F1F5F9;
            --text-secondary: #E2E8F0;
            --text-muted: #9CA3AF;
            --border: rgba(255,255,255,0.1);
            --border-light: rgba(255,255,255,0.05);
            --gold: #FFD700;
        }"""

content = re.sub(r'\[data-theme="dark"\]\s*\{[^}]+\}', dark_mode_replacement, content)

# Enable dark mode by default if not already
content = content.replace('<html lang="en">', '<html lang="en" data-theme="dark">')

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated CSS Variables to V4 Design System successfully.")
