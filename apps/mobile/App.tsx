import { StatusBar } from 'expo-status-bar';
import { StyleSheet, Text, View } from 'react-native';

// Placeholder root screen. Real navigation (record -> review/sign,
// patient search, loose-sessions tray) lands with the first feature
// slice — see docs/tech-stack.md for the client architecture this
// scaffold is built against.
export default function App() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>Remedy Scribe</Text>
      <Text style={styles.subtitle}>In-clinic AI consultation note-taker</Text>
      <StatusBar style="auto" />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: '#fff',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
  },
  title: {
    fontSize: 20,
    fontWeight: '600',
  },
  subtitle: {
    fontSize: 14,
    color: '#555',
  },
});
