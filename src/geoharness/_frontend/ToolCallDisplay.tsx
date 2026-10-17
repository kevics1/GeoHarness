import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';

export function resultLineCount(text: string): number {
	return text.split('\n').filter((line) => line.trim()).length;
}

export function ToolCallDisplay({item, resultItem, outputStyle}: {
	item: TranscriptItem;
	resultItem?: TranscriptItem;
	outputStyle?: string;
}): React.JSX.Element {
	const {theme} = useTheme();
	const codex = outputStyle === 'codex';
	// Orphan results (including restored sessions) remain compact as well.
	if (item.role === 'tool_result') {
		if (!item.is_error) return <></>;
		return <Text dimColor>{item.tool_name ?? 'tool'} <Text color={theme.colors.error}>×</Text>{` → ${resultLineCount(item.text)}L`}</Text>;
	}
	if (item.role !== 'tool') return <Text>{item.text}</Text>;
	const name = item.tool_name ?? 'tool';
	const summary = summarizeInput(name, item.tool_input, item.text).replace(/\s+/g, ' ').trim();
	let status: React.ReactNode = null;
	if (resultItem) {
		const count = resultLineCount(resultItem.text);
		status = resultItem.is_error ? <>
			<Text color={theme.colors.error}> ×</Text>
			<Text dimColor>{` → ${count}L`}</Text>
		</> : <Text dimColor>{codex ? (count > 0 ? ` ${count}L` : '') : ` → ${count > 0 ? `${count}L` : theme.icons.success.trim()}`}</Text>;
	}
	return <Box marginLeft={codex ? 0 : 2} flexDirection="column">
		<Text>
			<Text color={theme.colors.accent} bold={!codex}>{codex ? '• Ran ' : theme.icons.tool}{name}</Text>
			<Text dimColor>{summary ? ` ${summary}` : ''}</Text>{status}
		</Text>
	</Box>;
}

function summarizeInput(name: string, input?: Record<string, unknown>, fallback?: string): string {
	if (!input) return fallback?.slice(0, 80) ?? '';
	const lower = name.toLowerCase();
	if (lower === 'bash' && input.command) return String(input.command).slice(0, 120);
	if (['read', 'fileread', 'write', 'filewrite', 'edit', 'fileedit'].includes(lower) && input.file_path) return String(input.file_path);
	if (lower === 'grep' && input.pattern) return `/${String(input.pattern)}/`;
	if (lower === 'glob' && input.pattern) return String(input.pattern);
	if (lower === 'agent' && input.description) return String(input.description);
	const first = Object.entries(input)[0];
	return first ? `${first[0]}=${String(first[1]).slice(0, 60)}` : fallback?.slice(0, 80) ?? '';
}
