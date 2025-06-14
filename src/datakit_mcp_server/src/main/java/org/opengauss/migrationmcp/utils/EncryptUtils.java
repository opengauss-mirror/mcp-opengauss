package org.opengauss.migrationmcp.utils;

import javax.crypto.BadPaddingException;
import javax.crypto.Cipher;
import javax.crypto.IllegalBlockSizeException;
import javax.crypto.NoSuchPaddingException;
import java.security.*;
import java.security.spec.InvalidKeySpecException;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;

public class EncryptUtils {
    private static final String PUBLIC_KEY = "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDxRJAwJGMHBOKHW3nHFHaXylHy" +
            "t+ZrXNDOsbbZqCwgfTGrE9Jw6A+WQY/jiDEqThuMKLhb7z8//w6fmPu0kLdAmc1n" +
            "qkmozOOVt+1BgRqsVckJjw7eiRflqsDZEIgtnUMX61eyMcIGPHzxdL3XmlaPf1Qp" +
            "gPXBWQaNl4I7qJ2LoQIDAQAB";

    /**
     * 加密DataKit用户密码
     *
     * @param originalPassword 原始密码
     * @return 加密后的密码
     */
    public static String encrypt(String originalPassword) {
        try {
            byte[] publicKeyBytes = Base64.getDecoder().decode(PUBLIC_KEY);
            X509EncodedKeySpec keySpec = new X509EncodedKeySpec(publicKeyBytes);
            KeyFactory keyFactory = KeyFactory.getInstance("RSA");
            PublicKey publicKey = keyFactory.generatePublic(keySpec);

            Cipher cipher = Cipher.getInstance("RSA/ECB/PKCS1Padding");
            cipher.init(Cipher.ENCRYPT_MODE, publicKey);
            byte[] encryptedBytes = cipher.doFinal(originalPassword.getBytes());

            return Base64.getEncoder().encodeToString(encryptedBytes);
        } catch (NoSuchAlgorithmException | NoSuchPaddingException | IllegalBlockSizeException |
                 InvalidKeySpecException | BadPaddingException | InvalidKeyException e) {
            throw new RuntimeException("DataKit用户密码加密失败", e);
        }
    }
}
