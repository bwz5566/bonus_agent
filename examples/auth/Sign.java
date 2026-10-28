// 服务间鉴权签名示例（Java 11+），与 sign.py 的待签串完全一致。
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class Sign {
    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    // 与 Python urlencode(sorted(parse_qsl(q))) 一致：按 (名, 值) 排序，空格编码为 +
    static String canonicalQuery(String query) {
        List<String[]> kv = new ArrayList<>();
        if (query != null && !query.isEmpty()) {
            for (String p : query.split("&")) {
                if (p.isEmpty()) continue;
                int i = p.indexOf('=');
                String k = i < 0 ? p : p.substring(0, i), v = i < 0 ? "" : p.substring(i + 1);
                kv.add(new String[]{URLDecoder.decode(k, StandardCharsets.UTF_8), URLDecoder.decode(v, StandardCharsets.UTF_8)});
            }
        }
        kv.sort(Comparator.<String[], String>comparing(a -> a[0]).thenComparing(a -> a[1]));
        StringJoiner j = new StringJoiner("&");
        for (String[] a : kv)
            j.add(URLEncoder.encode(a[0], StandardCharsets.UTF_8) + "=" + URLEncoder.encode(a[1], StandardCharsets.UTF_8));
        return j.toString();
    }

    static String signature(String secret, String method, String path, String query, byte[] body,
                            String appId, String userId, String scopes, String ts, String nonce) throws Exception {
        String bodyHash = hex(MessageDigest.getInstance("SHA-256").digest(body));
        String text = String.join("\n", method.toUpperCase(), path, canonicalQuery(query), appId, userId, scopes, ts, nonce, bodyHash);
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        return Base64.getEncoder().encodeToString(mac.doFinal(text.getBytes(StandardCharsets.UTF_8)));
    }

    public static Map<String, String> headers(String secret, String method, String path, String query, byte[] body,
                                              String appId, String userId, String scopes) throws Exception {
        String ts = String.valueOf(System.currentTimeMillis() / 1000);
        byte[] n = new byte[16];
        new SecureRandom().nextBytes(n);
        String nonce = hex(n);
        String sig = signature(secret, method, path, query, body, appId, userId, scopes, ts, nonce);
        Map<String, String> h = new LinkedHashMap<>();
        h.put("X-App-Id", appId); h.put("X-User-Id", userId); h.put("X-User-Scopes", scopes);
        h.put("X-Timestamp", ts); h.put("X-Nonce", nonce); h.put("X-Signature", sig);
        return h;
    }
}
